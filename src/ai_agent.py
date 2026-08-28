import json
import logging
import os
import requests
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

try:
    from src.news_fetcher import NewsFetcher
except ImportError:
    try:
        from news_fetcher import NewsFetcher
    except ImportError:
        NewsFetcher = None  # type: ignore

logger = logging.getLogger("autotrader.ai_agent")


class LocalAIAgent:
    """LM Studio (OpenAI 호환 API)를 연동하는 로컬 AI 에이전트

    v3.1 학습 기능 추가:
      - 매수/매도 시점의 시장 스냅샷 기록
      - 일일 결산 시 AI가 당일 매매를 복기하고 교훈 추출
      - 추출된 교훈을 다음 매매 판단에 프롬프트로 주입 (지속적 학습)
    """

    def __init__(self, config: dict):
        ai_cfg = config.get("ai", {})
        self.enabled = bool(ai_cfg.get("enabled", False))
        self.endpoint = ai_cfg.get("endpoint", "http://127.0.0.1:1234/v1")
        self.model = ai_cfg.get("model", "google/gemma-4-e4b:2")
        self.temperature = float(ai_cfg.get("temperature", 0.0))
        self.min_confidence = int(ai_cfg.get("min_confidence", 65))
        self.context_trades = int(ai_cfg.get("context_trades", 5))

        # ── 학습 데이터 저장 경로 ──────────────────────────────
        self.data_dir = Path(ai_cfg.get("data_dir", "data/ai"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lessons_file = self.data_dir / "lessons.json"
        self.snapshots_file = self.data_dir / "snapshots.json"

        # ── 학습된 교훈 로드 ──────────────────────────────────
        self.lessons: List[str] = self._load_lessons()
        self.snapshots: List[Dict] = self._load_snapshots()

        # ── 뉴스 수집기 초기화 ────────────────────────────────
        self.news_fetcher = NewsFetcher(cache_ttl_sec=600) if NewsFetcher else None

        if self.lessons:
            logger.info("[AI 학습] 이전 교훈 %d건 로드됨", len(self.lessons))

    # ── 교훈 저장/로드 ────────────────────────────────────────
    def _load_lessons(self) -> List[str]:
        try:
            if self.lessons_file.exists():
                with open(self.lessons_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("lessons", [])[-10:]  # 최근 10개만
        except Exception as e:
            logger.warning("[AI 학습] 교훈 로드 실패: %s", e)
        return []

    def _save_lessons(self) -> None:
        try:
            with open(self.lessons_file, "w", encoding="utf-8") as f:
                json.dump({"lessons": self.lessons[-10:],
                           "updated": datetime.now().isoformat()}, f,
                          ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[AI 학습] 교훈 저장 실패: %s", e)

    # ── 시장 스냅샷 저장/로드 ─────────────────────────────────
    def _load_snapshots(self) -> List[Dict]:
        try:
            if self.snapshots_file.exists():
                with open(self.snapshots_file, "r", encoding="utf-8") as f:
                    return json.load(f).get("snapshots", [])[-20:]
        except Exception as e:
            logger.warning("[AI 학습] 스냅샷 로드 실패: %s", e)
        return []

    def _save_snapshots(self) -> None:
        try:
            with open(self.snapshots_file, "w", encoding="utf-8") as f:
                json.dump({"snapshots": self.snapshots[-20:],
                           "updated": datetime.now().isoformat()}, f,
                          ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[AI 학습] 스냅샷 저장 실패: %s", e)

    # ── 매수/매도 시점 시장 스냅샷 기록 ───────────────────────
    def capture_market_snapshot(
        self,
        symbol: str,
        side: str,
        price: float,
        score_info: str,
        reason: str = "",
        vwap: float = 0.0,
        ma5: Optional[float] = None,
        ma20: Optional[float] = None,
        vol_ratio: float = 0.0,
        dip_pct: float = 0.0,
    ) -> None:
        """매수/매도 시점의 시장 상황을 기록 (학습용)"""
        snapshot = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "side": side,
            "price": price,
            "score": score_info,
            "reason": reason,
            "vwap": round(vwap, 1),
            "ma5": round(ma5, 1) if ma5 else None,
            "ma20": round(ma20, 1) if ma20 else None,
            "vol_ratio": round(vol_ratio, 2),
            "dip_pct": round(dip_pct, 2),
        }
        self.snapshots.append(snapshot)
        self._save_snapshots()
        logger.info("[AI 학습] 시장 스냅샷 기록: %s %s @%.0f (%s)", side, symbol, price, reason)

    # ── 진입 평가 (뉴스 + 교훈 반영) ─────────────────────────
    def evaluate_entry(
        self,
        symbol: str,
        current_price: float,
        score_info: str,
        recent_trades: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {"trade": True, "confidence": 100, "reason": "AI 비활성화 (기본 승인)"}

        # 최근 매매 기록 요약
        trades_summary = "최근 매매 기록 없음."
        if recent_trades:
            lines = []
            for t in recent_trades:
                pnl = float(t.get("pnl", 0) or 0)
                res = "수익" if pnl > 0 else "손실"
                lines.append(f"- {t.get('symbol')}: {res} ({pnl:.0f}원)")
            trades_summary = "\n".join(lines)

        # 학습된 교훈 주입
        lessons_text = "아직 학습된 교훈 없음."
        if self.lessons:
            lessons_text = "\n".join(f"- {l}" for l in self.lessons[-5:])

        # ── 실시간 뉴스 수집 (국내: 네이버 / 해외: yfinance) ──
        news_text = "뉴스 없음 또는 수집 불가."
        if self.news_fetcher:
            try:
                headlines = self.news_fetcher.get_news(symbol, limit=5)
                if headlines:
                    news_text = "\n".join(f"• {h}" for h in headlines)
                    logger.info("[AI 뉴스] %s 뉴스 %d건 수집 완료", symbol, len(headlines))
                else:
                    news_text = "관련 뉴스 없음."
            except Exception as e:
                logger.warning("[AI 뉴스] %s 뉴스 수집 실패: %s", symbol, e)

        system_prompt = (
            "당신은 '눌림목 매수' 자동매매 시스템의 최종 승인(필터링) 역할을 맡은 AI입니다.\n"
            "전체적으로 올라갈 만한 종목(상승 추세)이 일시적으로 지지선(VWAP/MA5)까지 떨어졌을 때 매수하는 전략입니다.\n"
            "핵심 판단 기준:\n"
            "1. 상승 추세가 유효한지 (20MA 위, 정배열)\n"
            "2. 현재 가격이 지지선(VWAP/MA5) 근처에서 반등 가능한 위치인지\n"
            "3. 눌림이 단순 조정인지 추세 전환인지 (거래량 감소=건강한 조정)\n"
            "4. [실시간 뉴스]에 악재(업황악화·소송·매출급감·시장충격)가 있으면 FALSE\n"
            "5. 아래 [과거 학습 교훈]을 반드시 참고하여 같은 실수를 반복하지 마세요\n"
            "반드시 아래 JSON 형식으로만 응답해야 합니다.\n"
            '{"trade": true/false, "confidence": 0~100, "reason": "한글로 20자 이내 설명"}'
        )

        user_prompt = f"""
[현재 상황]
종목: {symbol}
현재가: {current_price}
눌림목 평가: {score_info}

[실시간 뉴스 (최근 5건)]
{news_text}

[최근 매매 기록 ({self.context_trades}건)]
{trades_summary}

[과거 학습 교훈]
{lessons_text}

위 상황, 뉴스, 과거 교훈을 종합하여 현재 눌림목에서 매수 진입할지 JSON으로 결정해 주세요.
특히 뉴스에 악재가 있다면 trade: false로 즉시 거절하세요.
"""

        return self._call_ai(system_prompt, user_prompt, symbol)

    # ── 일일 결산 학습 (매매 복기 + 교훈 추출) ────────────────
    def daily_review(
        self,
        trades: List[Dict],
        snapshots: Optional[List[Dict]] = None,
        symbol_names: Optional[Dict[str, str]] = None,
    ) -> str:
        """당일 매매를 AI가 복기하고 손실 원인/판단 미스를 분석하여 교훈을 저장

        Args:
            trades: 당일 매매 기록 (journal.get_today_trades())
            snapshots: 당일 시장 스냅샷
            symbol_names: 종목코드-종목명 매핑 딕셔너리

        Returns:
            AI의 복기 및 손실 분석 결과 텍스트 (텔레그램 발송용)
        """
        if not self.enabled:
            return ""

        sell_trades = [t for t in trades if t.get("side") == "SELL"]
        if not sell_trades:
            return ""

        def _fmt_sym(sym: str) -> str:
            if symbol_names:
                name = symbol_names.get(sym, "")
                if name and name != sym:
                    return f"{sym}({name})"
            return sym

        # 매매 요약 구성
        trade_lines = []
        for t in sell_trades:
            raw_sym = str(t.get("symbol", ""))
            lbl = _fmt_sym(raw_sym)
            pnl = float(t.get("pnl", 0) or 0)
            pnl_pct = float(t.get("pnl_pct", 0) or 0)
            result = "✅수익" if pnl > 0 else "❌손실"
            trade_lines.append(
                f"- {lbl} | {result} {pnl:+,.0f}원 ({pnl_pct:+.2f}%) | "
                f"진입가:{t.get('entry_price')} 청산가:{t.get('price')} | "
                f"사유:{t.get('exit_reason', '')}"
            )
        trade_summary = "\n".join(trade_lines)

        # 스냅샷 요약
        snap_text = "시장 스냅샷 없음."
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_snaps = snapshots or [s for s in self.snapshots
                                     if s.get("time", "").startswith(today_str)]
        if today_snaps:
            snap_lines = []
            for s in today_snaps:
                raw_sym = str(s.get("symbol", ""))
                lbl = _fmt_sym(raw_sym)
                snap_lines.append(
                    f"- [{s.get('side')}] {lbl} @{s.get('price')} "
                    f"VWAP:{s.get('vwap')} MA5:{s.get('ma5')} MA20:{s.get('ma20')} "
                    f"거래량비:{s.get('vol_ratio')} 눌림:{s.get('dip_pct')}% "
                    f"스코어:{s.get('score')} 사유:{s.get('reason')}"
                )
            snap_text = "\n".join(snap_lines)

        # 기존 교훈 텍스트
        existing_lessons = "\n".join(f"- {l}" for l in self.lessons[-5:]) if self.lessons else "없음"

        system_prompt = (
            "당신은 자동매매 시스템의 매매 복기 및 반성 학습 담당 AI입니다.\n"
            "오늘의 매매 기록과 매수/매도 시점의 시장 상황을 분석하여:\n"
            "1. 전체 복기 요약(review)과 일일 평점(grade: A/B/C/D/F)을 평가하세요.\n"
            "2. 특히 손실이 발생한 거래(pnl < 0)에 대해 어떤 판단 미스(고점 추격, 지지선 이탈 방치, 거래량 부족, 호가 얇음 등)가 발생했는지 개별 종목별 원인을 분석하세요.\n"
            "3. 동일한 판단 미스를 되풀이하지 않도록 내일 매매에 적용할 구체적인 행동 지침 교훈(lessons)을 3~5개 추출하세요.\n"
            "반드시 아래 JSON 형식으로만 응답해야 합니다.\n"
            '{"review": "전체 복기 요약 (100자 이내)", '
            '"loss_analysis": ["종목명: 손실 원인 및 판단 미스 내용", ...], '
            '"lessons": ["교훈1", "교훈2", "교훈3"], '
            '"grade": "A/B/C/D/F"}'
        )

        user_prompt = f"""
[오늘 매매 기록]
{trade_summary}

[매수/매도 시점 시장 상황]
{snap_text}

[기존 학습 교훈]
{existing_lessons}

위 내용을 바탕으로 오늘 매매를 복기하고, 손실 발생 거래의 판단 미스 분석과 내일 적용할 교훈을 JSON으로 추출해 주세요.
"""

        try:
            result = self._call_ai_raw(system_prompt, user_prompt, timeout=10.0)
            if not result:
                return ""

            data = json.loads(result)
            review = data.get("review", "")
            loss_analysis = data.get("loss_analysis", [])
            new_lessons = data.get("lessons", [])
            grade = data.get("grade", "?")

            # 새 교훈 및 판단 미스 원인을 기존 교훈 목록에 추가 (중복 제거, 최대 10개)
            combined_lessons = []
            if isinstance(loss_analysis, list):
                for item in loss_analysis:
                    if item and isinstance(item, str):
                        combined_lessons.append(f"손실원인: {item}")
            if isinstance(new_lessons, list):
                for item in new_lessons:
                    if item and isinstance(item, str):
                        combined_lessons.append(item)

            for lesson in combined_lessons:
                if lesson and lesson not in self.lessons:
                    self.lessons.append(lesson)
            self.lessons = self.lessons[-10:]
            self._save_lessons()

            # 당일 스냅샷 정리
            self.snapshots = []
            self._save_snapshots()

            loss_lines = []
            if isinstance(loss_analysis, list) and loss_analysis:
                loss_lines = [f"• {item}" for item in loss_analysis if item]

            report_parts = [
                f"🧠 <b>AI 매매 복기 및 학습</b> (등급: {grade})\n{review}"
            ]
            if loss_lines:
                report_parts.append("❌ <b>손실 원인 및 판단 미스 분석</b>\n" + "\n".join(loss_lines))

            if new_lessons:
                report_parts.append("📝 <b>내일 적용할 학습 교훈</b>\n" + "\n".join(f"• {l}" for l in new_lessons[:5]))

            report = "\n\n".join(report_parts)

            logger.info("[AI 학습] 일일 복기 완료 (등급:%s, 손실분석:%d건, 새 교훈:%d건)", grade, len(loss_lines), len(new_lessons))
            return report

        except Exception as e:
            logger.error("[AI 학습] 일일 복기 실패: %s", e)
            return ""

    # ── 내부: AI API 호출 ─────────────────────────────────────
    def _call_ai(self, system_prompt: str, user_prompt: str, symbol: str = "") -> Dict[str, Any]:
        try:
            raw = self._call_ai_raw(system_prompt, user_prompt, timeout=3.0)
            if not raw:
                return {"trade": False, "confidence": 0, "reason": "AI 응답 없음"}

            result = json.loads(raw)
            trade = bool(result.get("trade", False))
            confidence = int(result.get("confidence", 0))
            reason = str(result.get("reason", "판단 사유 없음"))

            # v3.2: 확신도 임계값(65%) 미달 시 승인 거절
            if trade and confidence < self.min_confidence:
                trade = False
                reason = f"확신도부족({confidence}%<{self.min_confidence}%)"

            logger.info("[AI 판단] %s | 승인:%s 확신도:%d%% 사유:%s", symbol, trade, confidence, reason)
            return {"trade": trade, "confidence": confidence, "reason": reason}

        except requests.exceptions.Timeout:
            logger.warning("[AI 판단] %s | 타임아웃 → 규칙 기반 승인으로 대체", symbol)
            return {"trade": True, "confidence": 50, "reason": "AI 타임아웃 — 규칙 기반 통과"}
        except Exception as e:
            logger.warning("[AI 판단] %s | 에러(%s) → 규칙 기반 승인으로 대체", symbol, e)
            return {"trade": True, "confidence": 50, "reason": "AI 에러 — 규칙 기반 통과"}

    def _call_ai_raw(self, system_prompt: str, user_prompt: str, timeout: float = 3.0) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": self.temperature,
        }
        resp = requests.post(f"{self.endpoint}/chat/completions", json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        raw_content = data["choices"][0]["message"]["content"]

        # JSON 블록 추출 (```json ... ``` 또는 { ... } 직접)
        import re
        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw_content, re.DOTALL)
        if json_match:
            return json_match.group(1)
        json_match = re.search(r'(\{[^{}]*\})', raw_content, re.DOTALL)
        if json_match:
            return json_match.group(1)
        return raw_content

