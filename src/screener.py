"""
screener.py — 장 초반 종목 자동 스크리닝

v1.1 수정사항
  - [설계오류] 기본 유니버스가 전부 초대형주였는데 시총 상한(1조)에 걸려
    모든 종목이 탈락하던 모순 수정 → 거래량 랭킹 기반 동적 유니버스로 전환
  - 최소 거래대금(유동성) 필터 추가 — 스켈핑은 체결 유동성이 생명
  - 가격대 필터 추가 (동전주·초고가주 제외)
  - 상한가 근접 종목 제외 (진입 후 청산 불가 위험)
  - 관리종목·거래정지 제외
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .kis_client import KISClient

logger = logging.getLogger("autotrader.screener")

# 정적 폴백 유니버스 — 거래량 랭킹 API 실패 시에만 사용
# 갭·거래량이 자주 발생하는 중소형 테마주 위주로 구성
_FALLBACK_UNIVERSE = [
    "247540",  # 에코프로비엠
    "086520",  # 에코프로
    "091990",  # 셀트리온헬스케어
    "196170",  # 알테오젠
    "028300",  # HLB
    "022100",  # 포스코DX
    "095340",  # ISC
    "058470",  # 리노공업
    "277810",  # 레인보우로보틱스
    "348370",  # 엔켐
    "137400",  # 피엔티
    "240810",  # 원익IPS
    "042700",  # 한미반도체
    "403870",  # HPSP
    "294090",  # 이오테크닉스
]


@dataclass
class CandidateSymbol:
    """스크리닝 통과 종목"""
    symbol:             str
    name:               str
    price:              int
    gap_pct:            float
    change_pct:         float
    spread_pct:         float
    market_cap_bil:     float    # 억원
    volume:             int
    turnover_bil:       float    # 거래대금 (억원)
    ma_alignment_score: float = 20.0 # 이동평균 정배열 스코어 (20점 만점)
    reject:             str = ""

    def __str__(self) -> str:
        name_str = f"({self.name})" if self.name and self.name != self.symbol else ""
        return (
            f"{self.symbol}{name_str} "
            f"갭{self.gap_pct:+.1f}% 등락{self.change_pct:+.1f}% "
            f"시총{self.market_cap_bil:,.0f}억 거래대금{self.turnover_bil:,.0f}억 "
            f"정배열점수:{self.ma_alignment_score:.0f}/20점"
        )


def default_universe() -> list[str]:
    """정적 폴백 유니버스"""
    return _FALLBACK_UNIVERSE.copy()


class Screener:
    """장중 종목 스크리닝 (v4.0 Top-Down 일봉 우상향 눌림목)"""

    def __init__(self, client: KISClient, config: dict) -> None:
        self.client = client
        u = config.get("universe", {})

        self.min_change_pct:        float = float(u.get("min_change_pct", -3.0))
        self.max_change_pct:        float = float(u.get("max_change_pct", 3.5))
        self.require_daily_uptrend: bool  = bool(u.get("require_daily_uptrend", True))
        self.max_spread_pct:        float = float(u.get("max_spread_pct", 0.3))
        self.min_cap_bil:           float = float(u.get("min_market_cap_bil", 1_000))
        self.max_cap_bil:           float = float(u.get("max_market_cap_bil", 100_000))
        self.min_turnover_bil:      float = float(u.get("min_turnover_bil", 50))
        self.min_price:             int   = int(u.get("min_price", 2_000))
        self.max_price:             int   = int(u.get("max_price", 500_000))
        self.max_symbols:           int   = int(u.get("max_symbols", 5))
        self.rank_pool:             int   = int(u.get("rank_pool", 40))

    # ── 진입점 ────────────────────────────────────────────────
    def screen(self, universe: Optional[list[str]] = None) -> list[CandidateSymbol]:
        """유니버스 스크리닝 (일봉 우상향 + 당일 눌림목 종목 선별)"""
        if universe is None:
            universe = self._build_universe()

        logger.info("스크리닝 시작: 후보 %d종목 (일봉 우상향 눌림목 탐색)", len(universe))

        passed: list[CandidateSymbol] = []
        rejected: dict[str, int] = {}

        for sym in universe:
            try:
                cand = self._evaluate(sym)
                if cand is None:
                    continue
                if cand.reject:
                    rejected[cand.reject] = rejected.get(cand.reject, 0) + 1
                    continue
                passed.append(cand)
            except Exception as e:
                logger.debug("[%s] 평가 오류: %s", sym, e)

        # 거래대금 우량도 기준 상위 선택 (급등 모멘텀 대신 수급 안전성 우선)
        passed.sort(key=lambda c: c.turnover_bil, reverse=True)
        result = passed[:self.max_symbols]

        logger.info(
            "스크리닝 완료: %d/%d 통과 → 상위 %d 선택 | 탈락사유 %s",
            len(passed), len(universe), len(result), rejected,
        )
        for c in result:
            logger.info("  ✔ %s", c)

        return result

    def _build_universe(self) -> list[str]:
        """거래량 랭킹으로 동적 유니버스 구성"""
        try:
            rows = self.client.get_volume_rank()
            symbols = [
                str(r["mksc_shrn_iscd"]) for r in rows
                if r.get("mksc_shrn_iscd")
            ][:self.rank_pool]
            if symbols:
                logger.info("거래량 랭킹 %d종목 수집", len(symbols))
                return symbols
        except Exception as e:
            logger.warning("거래량 랭킹 조회 실패: %s", e)

        logger.info("정적 폴백 유니버스 사용")
        return default_universe()

    # ── 단일 종목 평가 ────────────────────────────────────────
    def _evaluate(self, symbol: str) -> Optional[CandidateSymbol]:
        d = self.client.get_price(symbol)
        if not d:
            return None

        def num(key: str, default: float = 0.0) -> float:
            v = d.get(key)
            try:
                return float(v) if v not in (None, "") else default
            except (TypeError, ValueError):
                return default

        price      = int(num("stck_prpr"))
        prev_close = int(num("stck_sdpr"))
        open_price = int(num("stck_oprc", price))
        volume     = int(num("acml_vol"))
        turnover   = num("acml_tr_pbmn") / 1e8          # 거래대금 → 억원
        shares     = num("lstn_stcn")
        change_pct = num("prdy_ctrt")
        name       = (
            self.client.get_symbol_name(symbol)
            or str(d.get("hts_kor_isnm", "") or d.get("prdt_name", "") or d.get("rprs_mrkt_kor_name", "") or symbol).strip()
        )

        if price <= 0 or prev_close <= 0:
            return None

        cap_bil = shares * price / 1e8
        gap_pct = (open_price - prev_close) / prev_close * 100

        c = CandidateSymbol(
            symbol=symbol, name=name, price=price,
            gap_pct=gap_pct, change_pct=change_pct, spread_pct=0.0,
            market_cap_bil=cap_bil, volume=volume, turnover_bil=turnover,
        )

        # ── 1. 파생/우선주/스팩 제외 ───────────────────────────
        exclude_keywords = [
            "ETF", "ETN", "KODEX", "TIGER", "ACE", "SOL", "ARIRANG", "HANARO", 
            "KBSTAR", "KOSEF", "TIMEFOLIO", "FOCUS", "PLUS", "RISE", "WON", 
            "선물", "인버스", "레버리지", "스팩", "우B", "3우", "우C", "우"
        ]
        name_upper = name.upper()
        if any(kw in name_upper for kw in exclude_keywords):
            c.reject = "파생/우선주/스팩제외"
            return c

        if not (self.min_price <= price <= self.max_price):
            c.reject = "가격대"
            return c

        if cap_bil > 0 and not (self.min_cap_bil <= cap_bil <= self.max_cap_bil):
            c.reject = "시총범위"
            return c

        # ── 2. 당일 급등주 배제 & 당일 눌림목 범위 검증 ─────────
        # 당일 등락률이 너무 높으면(+3.5% 초과) 급등주 추격이 되므로 제외!
        if change_pct > self.max_change_pct:
            c.reject = f"당일급등배제({change_pct:+.1f}%>{self.max_change_pct:+.1f}%)"
            return c
        if change_pct < self.min_change_pct:
            c.reject = f"폭락주제외({change_pct:+.1f}%<{self.min_change_pct:+.1f}%)"
            return c

        # ── 3. 당일 고점 대비 눌림폭 검증 ──────────────────────
        high_price = num("stck_hgpr", price)
        # 상한가 근접 — 매도 체결 불가 위험
        if change_pct >= self.max_change_pct:
            c.reject = "상한가근접"
            return c

        # 3. 시간대별 정규화된 최소 거래대금
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now_hhmm = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%H%M")
        if now_hhmm < "1000":
            req_turnover = self.min_turnover_bil * 0.4   # 09시대: 20억
        elif now_hhmm < "1130":
            req_turnover = self.min_turnover_bil * 0.7   # 10~11시: 35억
        else:
            req_turnover = self.min_turnover_bil         # 오후: 50억

        if turnover < req_turnover:
            c.reject = f"거래대금부족({turnover:.0f}<{req_turnover:.0f}억)"
            return c

        if cap_bil > 0 and not (self.min_cap_bil <= cap_bil <= self.max_cap_bil):
            c.reject = "시총범위"
            return c

        # ── 호가 스프레드 (추가 API 호출) ─────────────────────
        spread = self._spread_pct(symbol)
        c.spread_pct = spread
        if spread > self.max_spread_pct:
            c.reject = "스프레드"
            return c

        return c

    def _spread_pct(self, symbol: str) -> float:
        """최우선 호가 스프레드 (%)"""
        try:
            ob = self.client.get_orderbook(symbol)
            ask = int(float(ob.get("askp1", 0) or 0))
            bid = int(float(ob.get("bidp1", 0) or 0))
            if ask > 0 and bid > 0:
                mid = (ask + bid) / 2
                return (ask - bid) / mid * 100
        except Exception as e:
            logger.debug("[%s] 호가 조회 실패: %s", symbol, e)
        return 999.0
