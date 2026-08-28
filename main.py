"""
main.py — 공격적 스켈핑 자동매매 오케스트레이터

v2.0 수정사항 (config v2.0 대응)
  - 폴링 주기 분리: 보유 포지션이 있으면 holding_interval_sec 로 가속
                   (손절폭 1~2%대에서 15초 폴링은 슬리피지가 손절폭을 잠식)
  - 장 마감 분할 청산: force_close_start ~ force_close 구간을 등분해 분산 청산
                      (전 종목 동시 시장가는 슬리피지가 큼)
  - 리스크 설정 정합성 검증: daily_loss_limit_pct 가 실제로 발동 가능한지 기동 시 점검
  - 왕복 거래비용 헬퍼 round_trip_cost_pct() 추가 (2026년 거래세 0.20% 반영)
  - v2.0 신규 파라미터의 src/ 모듈 지원 여부 기동 배너 출력

  ⚠️ 아래 항목은 src/ 모듈에 위치하므로 이 파일만으로는 적용되지 않습니다.
       · 본절 스톱 제거 / 즉시 트레일링   → src/strategy.py check_exit(), src/executor.py
       · ATR 기반 손절폭                  → src/indicators.py, src/executor.py
       · R배수 익절                       → src/strategy.py, src/executor.py
       · VWAP 이탈 종가 확정              → src/strategy.py check_exit()
       · 손절 시장가 주문                 → src/executor.py exit()
       · force_close_partial(ratio)       → src/executor.py (현재 미구현 — 폴백 동작)

v1.1 수정사항
  - [치명] ORB가 실제로 형성되지 않은 채 ready 처리되어 모든 종목이
          '항상 돌파' 판정되던 버그 수정 → 과거 분봉 일괄 로드로 ORB 사전 구축
  - [치명] 매 틱마다 동일 봉을 재누적해 VWAP/SMA가 오염되던 문제 수정
  - 시작 시 브로커 잔고 대사 (크래시 후 고아 포지션 복구)
  - 상태 스냅샷 저장으로 재시작 시 컨텍스트 유지
  - Ctrl+C 안전 종료 (미청산 포지션 경고)
  - 휴장일 자동 감지
  - 예외 발생 시 루프가 죽지 않도록 방어

실행:
    python main.py --dry-run --once   # 신호만 1회 확인
    python main.py                    # 실운용
    python main.py --backtest data/   # 백테스트
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
import traceback
from pathlib import Path
from datetime import datetime

import yaml
from dotenv import load_dotenv

from src.utils import (
    setup_logger, now_kst, now_hhmm, now_hhmmss,
    time_between, hhmm_ge, is_market_open, is_trading_day,
)
from src.auth import KISAuth
from src.kis_client import KISClient
from src.indicators import SymbolIndicators, parse_candles
from src.strategy import StrategyEngine, ScaleInSignal
from src.screener import Screener
from src.risk_manager import RiskManager
from src.executor import Executor
from src.journal import TradeJournal
from src.notifier import Notifier
from src.state import StateStore, reconcile_positions
from src.ai_agent import LocalAIAgent


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ──────────────────────────────────────────────────────────────
#  비용 / 시각 헬퍼  (v2.0 신규)
# ──────────────────────────────────────────────────────────────
def round_trip_cost_pct(config: dict) -> float:
    """왕복 거래비용 (%)

    매수 수수료 + 매도 수수료 + 매도 거래세 + 편도 슬리피지 × 2

    2026년 기준 거래세는 코스피(0.05% + 농특세 0.15%)와
    코스닥(0.20%) 모두 0.20% 로 동일하다.

    기본값 기준: 0.015×2 + 0.20 + 0.05×2 = 0.33%
    """
    cost = config.get("cost", {}) or {}
    commission = float(cost.get("commission_pct", 0.015))
    tax        = float(cost.get("tax_pct", 0.20))
    slippage   = float(cost.get("slippage_pct", 0.0))
    return commission * 2 + tax + slippage * 2


def _hhmm_to_min(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def _min_to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class AutoTrader:
    """스켈핑 자동매매 메인 루프"""

    def __init__(self, config: dict, dry_run: bool = False) -> None:
        self.config  = config
        self.dry_run = dry_run

        log_cfg = config.get("logging", {})
        self.logger = setup_logger(
            level=log_cfg.get("level", "INFO"),
            log_dir=log_cfg.get("log_dir", "logs"),
        )

        self.auth   = KISAuth()
        self.client = KISClient(
            self.auth,
            max_per_sec=int(config.get("polling", {}).get("max_req_per_sec", 8)),
        )

        self.strategy = StrategyEngine(config)
        self.journal  = TradeJournal(log_cfg.get("journal_dir", "data/journal"))
        self.notifier = Notifier()
        self.state    = StateStore(log_cfg.get("state_file", "data/state.json"))

        capital = self._fetch_capital()
        self.risk = RiskManager(config, capital)
        self.ai_agent = LocalAIAgent(config)

        self.executor = Executor(
            client=self.client, risk=self.risk, strategy=self.strategy,
            journal=self.journal, notifier=self.notifier,
            config=config, dry_run=dry_run, ai_agent=self.ai_agent,
        )
        self.screener = Screener(self.client, config)

        self.indicators: dict[str, SymbolIndicators] = {}

        strat = config.get("strategy", {})
        orb   = config.get("orb", {})
        self.orb_start     = orb.get("start_time", "09:00")
        self.orb_end       = orb.get("end_time",   "09:30")
        self.bar_minutes   = int(orb.get("candle_minutes", 5))
        self.trade_start   = strat.get("trading_start", "09:30")
        self.trade_end     = strat.get("trading_end",   "11:00")
        self.force_close   = strat.get("force_close",   "15:15")
        self.ma_short      = int(strat.get("ma_short", 5))
        self.ma_long       = int(strat.get("ma_long", 20))
        self.atr_period    = int(config.get("stop_loss", {}).get("atr_period", 14))  # v2.0

        # ── 폴링 (v2.0: 보유 시 가속) ─────────────────────────
        poll = config.get("polling", {})
        self.poll_interval = int(poll.get("price_interval_sec", 15))
        self.hold_interval = int(poll.get("holding_interval_sec", 0) or 0)

        # ── 장 마감 분할 청산 (v2.0) ──────────────────────────
        execu = config.get("execution", {})
        self.fc_start  = execu.get("force_close_start", "") or ""
        self.fc_slices = int(execu.get("force_close_slices", 1) or 1)
        self.fc_schedule = self._build_force_close_schedule()
        self._fc_done_idx = -1
        self._fc_partial_warned = False

        self.rescreen_interval_min = float(strat.get("rescreen_interval_min", 15))
        self.rescreen_golden_min   = float(strat.get("rescreen_golden_min", 5))
        self.rescreen_empty_min    = float(strat.get("rescreen_empty_min", 3))

        self._last_screen_time: datetime | None = None
        self._orb_finalized: set[str] = set()
        self._last_reset_date = ""
        self._closed_today    = False
        self._running         = True
        self._tick_errors     = 0

        self._validate_config()
        self._validate_risk_config()
        self._log_v2_support_banner()

        # 크래시 복구
        self._restore_and_reconcile()

    # ── 설정 정합성 검증 ──────────────────────────────────────
    def _validate_config(self) -> None:
        """지표 성숙 시각이 진입 창을 잠식하는지 검사

        ma_long × candle_minutes 만큼의 시간이 지나야 지표가 성숙한다.
        이 시각이 trading_start 보다 늦으면 그만큼 진입 기회를 잃는다.
        """
        from src.indicators import warmup_minutes

        need = warmup_minutes(self.ma_long, self.bar_minutes)
        h, m = int(self.orb_start[:2]), int(self.orb_start[3:5])
        total = h * 60 + m + need
        ready = f"{total // 60:02d}:{total % 60:02d}"

        self.logger.info(
            "지표 성숙 시각: %s (%d분봉 × %dMA = %d분)",
            ready, self.bar_minutes, self.ma_long, need,
        )

        if ready > self.trade_end:
            self.logger.critical(
                "설정 오류: 지표 성숙(%s)이 진입 종료(%s)보다 늦어 거래가 불가능합니다. "
                "config.yaml 의 orb.candle_minutes 또는 strategy.ma_long 을 줄이세요.",
                ready, self.trade_end,
            )
        elif ready > self.trade_start:
            lost = total - (int(self.trade_start[:2]) * 60 + int(self.trade_start[3:5]))
            self.logger.warning(
                "지표 성숙(%s)이 진입 시작(%s)보다 늦어 초기 %d분을 사용할 수 없습니다. "
                "orb.candle_minutes 를 줄이면 개선됩니다.",
                ready, self.trade_start, lost,
            )

        # ── ATR 손절 워밍업 검사 (v2.0) ───────────────────────
        sl = self.config.get("stop_loss", {}) or {}
        if sl.get("mode") == "atr":
            atr_need = int(sl.get("atr_period", 14)) + 1
            atr_ready = _min_to_hhmm(h * 60 + m + atr_need * self.bar_minutes)
            if atr_ready > self.trade_start:
                self.logger.warning(
                    "ATR(%d봉) 성숙 시각 %s 가 진입 시작(%s)보다 늦습니다. "
                    "그 전까지는 sl_pct(%.2f%%) 폴백이 사용됩니다.",
                    int(sl.get("atr_period", 14)), atr_ready, self.trade_start,
                    float(sl.get("sl_pct", 1.2)),
                )

        # ── 폴링 유량 개략 검사 (v2.0) ────────────────────────
        max_symbols = int(self.config.get("universe", {}).get("max_symbols", 5))
        interval = self.hold_interval or self.poll_interval
        if interval > 0:
            est_rps = max_symbols / interval
            limit = int(self.config.get("polling", {}).get("max_req_per_sec", 8))
            self.logger.info(
                "폴링: 감시 %d초 / 보유 %s초 — 추적 %d종목 기준 예상 %.1f req/s (한도 %d)",
                self.poll_interval, self.hold_interval or "-",
                max_symbols, est_rps, limit,
            )
            if est_rps > limit * 0.8:
                self.logger.warning(
                    "예상 호출량(%.1f req/s)이 한도(%d)의 80%%를 넘습니다. "
                    "KIS 계정 등급별 실제 유량 한도를 확인하고 모의투자에서 "
                    "429 오류 여부를 먼저 검증하세요.", est_rps, limit,
                )

    def _validate_risk_config(self) -> None:
        """리스크 한도가 실제로 발동 가능한 값인지 검사 (v2.0 신규)

        max_position_pct 가 실질 바인딩 제약이므로,
        risk_per_trade_pct 만 보고 한도를 정하면 영영 발동하지 않는
        '죽은 값'이 되기 쉽다.
        """
        risk = self.config.get("risk", {}) or {}
        sl   = self.config.get("stop_loss", {}) or {}

        cost = round_trip_cost_pct(self.config)
        pos_pct = float(risk.get("max_position_pct", 30.0)) / 100.0
        width = float(sl.get("sl_pct_min", sl.get("sl_pct", 1.0)))

        per_trade = pos_pct * (width + cost)
        n_daily   = int(risk.get("max_daily_trades", 8))
        n_consec  = int(risk.get("consecutive_loss_limit", 3))
        max_dd    = per_trade * n_daily
        consec_dd = per_trade * n_consec
        daily_lim = float(risk.get("daily_loss_limit_pct", 3.0))

        self.logger.info(
            "리스크 정합성: 왕복비용 %.2f%% | 거래당 실손실 ≈ %.2f%% | "
            "연속 %d회 ≈ %.2f%% | 이론상 일일 최대 ≈ %.2f%% | 일일한도 %.2f%%",
            cost, per_trade, n_consec, consec_dd, max_dd, daily_lim,
        )

        if daily_lim > max_dd:
            self.logger.warning(
                "daily_loss_limit_pct(%.2f%%)가 이론상 최대 일일 손실(%.2f%%)보다 커 "
                "절대 발동하지 않습니다. %.2f%% 이하를 권장합니다.",
                daily_lim, max_dd, max_dd * 0.6,
            )
        elif consec_dd >= daily_lim:
            self.logger.info(
                "연속 손실 한도(%.2f%%)가 일일 한도(%.2f%%)보다 먼저 발동합니다.",
                consec_dd, daily_lim,
            )

    def _log_v2_support_banner(self) -> None:
        """config v2.0 항목이 실제로 코드에 연결되어 있는지 기동 시 확인 (v2.0 신규)

        설정에는 켜져 있는데 코드가 지원하지 않으면 조용히 구버전 동작으로
        폴백되어, 의도한 개선이 적용되지 않은 채 운용될 수 있다.
        여기서 실제 객체의 속성/메서드를 확인해 상태를 명시한다.
        """
        sl = self.config.get("stop_loss", {}) or {}
        tp = self.config.get("take_profit", {}) or {}
        execu = self.config.get("execution", {}) or {}
        st = self.strategy

        import inspect
        exit_params = inspect.signature(st.check_exit).parameters

        # (설정 활성 여부, 라벨, 코드 지원 여부)
        checks = [
            (sl.get("mode") == "atr",
             "ATR 손절폭",
             getattr(st, "sl_mode", "") == "atr" and hasattr(st, "resolve_stop_pct")),
            (sl.get("trailing_arm_r") is not None,
             "R배수 트레일링",
             getattr(st, "trailing_arm_r", None) is not None),
            (sl.get("breakeven_after_tp1") is False,
             "본절 스톱 비활성",
             getattr(st, "breakeven_after_tp1", True) is False),
            (int(sl.get("vwap_stop_confirm_bars", 0) or 0) > 0,
             "VWAP 종가 확정 이탈",
             "vwap_breached" in exit_params),
            (tp.get("mode") == "r_multiple",
             "R배수 익절",
             getattr(st, "tp_mode", "") == "r_multiple"),
            (bool(tp.get("include_cost_in_r")),
             "R에 거래비용 내재화",
             getattr(st, "include_cost_in_r", False) is True),
            (execu.get("stop_order_type") == "01",
             "손절 시장가 주문",
             getattr(self.executor, "stop_order_type", "") == "01"),
            (int(execu.get("force_close_slices", 1) or 1) > 1,
             "장마감 분할 청산",
             hasattr(self.executor, "force_close_partial")),
        ]

        active = [(label, ok) for enabled, label, ok in checks if enabled]
        if not active:
            return

        missing = [label for label, ok in active if not ok]
        log = self.logger.warning if missing else self.logger.info

        log("─" * 56)
        log(" config v2.0 적용 상태")
        for label, ok in active:
            log("   %s %s", "✅" if ok else "❌", label)
        if missing:
            log(" ❌ 항목은 구버전 동작으로 폴백됩니다 — 코드 확인 필요")
        log(" 왕복 거래비용 %.2f%% | 실운용 전 백테스트로 기대값을 검증하세요.",
            round_trip_cost_pct(self.config))
        log("─" * 56)

    # ── 장 마감 분할 청산 스케줄 (v2.0) ───────────────────────
    def _build_force_close_schedule(self) -> list[str]:
        """force_close_start ~ force_close 를 force_close_slices 등분한 HHMM 리스트

        마지막 슬롯은 항상 force_close 시각이며 전량 청산한다.
        설정이 없거나 부적절하면 [force_close] 단일 슬롯으로 폴백한다.
        """
        t_end = _hhmm_to_min(self.force_close)

        if self.fc_slices <= 1 or not self.fc_start:
            return [self.force_close]

        t_start = _hhmm_to_min(self.fc_start)
        if t_start >= t_end:
            self.logger.warning(
                "force_close_start(%s)가 force_close(%s) 이후입니다 — 분할 청산 비활성화",
                self.fc_start, self.force_close,
            )
            return [self.force_close]

        step = (t_end - t_start) / (self.fc_slices - 1)
        sched = [_min_to_hhmm(int(round(t_start + step * i))) for i in range(self.fc_slices)]
        sched[-1] = self.force_close          # 최종 슬롯은 반드시 force_close
        sched = sorted(set(sched))            # 간격 1분 미만이면 중복 제거

        self.logger.info("장마감 분할 청산 스케줄: %s", " → ".join(sched))
        return sched

    # ── 초기화 ────────────────────────────────────────────────
    def _fetch_capital(self) -> float:
        try:
            equity = self.client.get_total_equity()
            if equity > 0:
                self.logger.info("초기 자본금(총평가): %.0f원", equity)
                return equity
            cash = self.client.get_cash()
            if cash > 0:
                return cash
        except Exception as e:
            self.logger.warning("자본금 조회 실패: %s", e)

        default = float(self.config.get("risk", {}).get("default_capital", 10_000_000))
        self.logger.warning("자본금 기본값 사용: %.0f원", default)
        return default

    def _restore_and_reconcile(self) -> None:
        """저장된 상태 복원 + 브로커 잔고 대사"""
        restored = self.state.restore(self.risk)
        if restored:
            self.logger.info("이전 세션 상태 복원됨")

        if self.dry_run:
            return

        try:
            holdings = self.client.get_holdings()
            warnings = reconcile_positions(self.risk, holdings, symbol_names=self.executor.symbol_names)
            if warnings:
                self.notifier.send("⚠️ 잔고 대사 경고\n" + "\n".join(warnings[:5]))
        except Exception as e:
            self.logger.error("잔고 대사 실패: %s", e)

    def _init_indicators(self, symbol: str) -> SymbolIndicators:
        if symbol not in self.indicators:
            self.indicators[symbol] = SymbolIndicators(
                symbol,
                ma_short=self.ma_short,
                ma_long=self.ma_long,
                orb_start=self.orb_start,
                orb_end=self.orb_end,
                atr_period=self.atr_period,      # v2.0
            )
        return self.indicators[symbol]

    # ── 봉 데이터 ─────────────────────────────────────────────
    def _load_history(self, symbol: str) -> int:
        """장 시작부터 현재까지 전체 분봉을 일괄 로드

        ORB 구간(09:00~09:30) 봉을 확보하기 위해 반드시 필요하다.
        SymbolIndicators 가 봉 시각으로 ORB를 판정하므로,
        09:30 이후에 호출해도 ORB가 정확히 재구성된다.
        """
        ind = self._init_indicators(symbol)
        try:
            rows = self.client.get_minute_candles_full(symbol)
            candles = parse_candles(
                rows, exclude_forming=True,
                now_hhmm=now_hhmm(), bar_minutes=self.bar_minutes,
            )
            added = ind.add_candles(candles)
            self.logger.info(
                "[%s] 분봉 로드: 수신 %d개 → 신규 %d개 (누적 %d봉)",
                self.executor._label(symbol), len(candles), added, len(ind.candles),
            )
            return added
        except Exception as e:
            self.logger.warning("[%s] 분봉 로드 실패: %s", self.executor._label(symbol), e)
            return 0

    def _update_candles(self, symbol: str) -> int:
        """최신 분봉만 증분 반영 (중복은 자동 무시)"""
        ind = self._init_indicators(symbol)
        try:
            rows = self.client.get_minute_candles(symbol, now_hhmmss())
            candles = parse_candles(
                rows, exclude_forming=True,
                now_hhmm=now_hhmm(), bar_minutes=self.bar_minutes,
            )
            return ind.add_candles(candles)
        except Exception as e:
            self.logger.warning("[%s] 분봉 갱신 실패: %s", symbol, e)
            return 0

    # ── 메인 루프 ─────────────────────────────────────────────
    def _current_interval(self) -> int:
        """폴링 간격 (v2.0)

        보유 포지션이 있으면 holding_interval_sec 로 가속한다.
        손절/트레일링 판정 지연이 곧 슬리피지이므로, 감시 상태보다
        보유 상태에서 훨씬 촘촘하게 봐야 한다.
        """
        if self.hold_interval > 0 and self.risk.positions:
            return self.hold_interval
        return self.poll_interval

    def run(self, once: bool = False) -> None:
        mode = "DRY-RUN" if self.dry_run else self.auth.mode.upper()
        self.logger.info("=" * 56)
        self.logger.info(" 자동매매 시작 [%s] 자본 %.0f원", mode, self.risk.capital)
        self.logger.info("=" * 56)
        self.notifier.send(f"🤖 자동매매 시작 [{mode}]\n자본: {self.risk.capital:,.0f}원")

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        try:
            while self._running:
                try:
                    self._tick()
                    self._tick_errors = 0
                except Exception as e:
                    self._tick_errors += 1
                    self.logger.error(
                        "틱 처리 오류 (%d회 연속): %s\n%s",
                        self._tick_errors, e, traceback.format_exc(),
                    )
                    if self._tick_errors >= 10:
                        self.logger.critical("연속 오류 10회 — 안전을 위해 중단")
                        self.notifier.send("🚨 연속 오류로 자동매매 중단")
                        break

                if once:
                    break
                time.sleep(self._current_interval())
        finally:
            self._shutdown()

    def _handle_signal(self, signum, frame) -> None:
        self.logger.info("종료 시그널 수신 (%s)", signum)
        self._running = False

    def _get_rescreen_interval(self) -> float:
        """동적 스크리닝 주기(분) 계산 (v3.2: 방안1 + 방안2)

        1. 추적 중인 종목이 0개인 경우: rescreen_empty_min (기본 3분)
        2. 장 초반 골든타임(09:30 ~ 10:30): rescreen_golden_min (기본 5분)
        3. 오후장(10:30 ~ 15:00): rescreen_interval_min (기본 15분)
        """
        if len(self.indicators) == 0:
            return self.rescreen_empty_min

        if time_between(self.trade_start, "10:30"):
            return self.rescreen_golden_min

        return self.rescreen_interval_min

    def _tick(self) -> None:
        """폴링 1회"""
        now = now_kst()
        today = now.strftime("%Y%m%d")

        # 휴장일
        if not is_trading_day(now.date()):
            self.logger.debug("휴장일 — 대기")
            return

        # 일일 초기화 (날짜가 바뀌면 1회)
        if self._last_reset_date != today:
            self.risk.reset_daily()
            self._last_screen_time = None
            self._closed_today   = False
            self._orb_finalized.clear()
            self.indicators.clear()
            self._fc_done_idx = -1
            self._last_reset_date = today

        if not is_market_open():
            return

        # ── 장 마감 분할 청산 (v2.0) ──────────────────────────
        if hhmm_ge(self.fc_schedule[0]):
            self._run_force_close()
            return

        # ── ORB 관찰 구간: 진입 없음 ──────────────────────────
        if time_between(self.orb_start, self.orb_end):
            for sym in list(self.indicators.keys()):
                self._update_candles(sym)
            return

        # ── 스크리닝 + 주기적 재검색 (v3.2: 방안1 + 방안2) ───────
        if hhmm_ge(self.trade_start) and time_between(self.trade_start, self.trade_end):
            interval = self._get_rescreen_interval()
            need_screen = False
            if self._last_screen_time is None:
                need_screen = True
            elif (now_kst() - self._last_screen_time).total_seconds() / 60 >= interval:
                need_screen = True
                
            if need_screen:
                self.logger.info("종목 스크리닝 시작 (적용 주기: %.1f분 | 감시 중: %d개)", interval, len(self.indicators))
                self._run_screening()
                self._last_screen_time = now_kst()

        # ── 지표 증분 갱신 ────────────────────────────────────
        for sym in list(self.indicators.keys()):
            self._update_candles(sym)

        # ── 보유 포지션 청산 판정 (진입보다 항상 우선) ────────
        self._manage_positions()

        # ── 보유 포지션 추가매수 탐색 (v2.1) ────────────────
        if time_between(self.trade_start, self.trade_end):
            self._scan_scale_in()

        # ── 신규 진입 탐색 ────────────────────────────────────
        if time_between(self.trade_start, self.trade_end):
            self._scan_entries()

        self.state.save(self.risk)

    # ── 장 마감 청산 (v2.0) ───────────────────────────────────
    def _run_force_close(self) -> None:
        """분할 강제 청산

        스케줄 슬롯마다 '남은 수량 / 남은 슬롯 수' 비율로 청산하고,
        최종 슬롯(force_close)에서는 반드시 전량 청산한다.
        오버나잇 포지션이 남으면 리스크 모델 전체가 무너지므로
        최종 슬롯의 전량 청산은 어떤 경우에도 건너뛰지 않는다.
        """
        sched = self.fc_schedule

        # 현재 도달한 가장 늦은 슬롯
        idx = -1
        for i, t in enumerate(sched):
            if hhmm_ge(t):
                idx = i
        if idx < 0:
            return

        is_final = (idx == len(sched) - 1)

        if self.risk.positions and idx > self._fc_done_idx:
            self._fc_done_idx = idx
            if is_final:
                self.logger.info("장마감 강제 청산 [최종 %s] — 잔량 전량", sched[idx])
                self.executor.force_close_all()
            else:
                remaining_slots = len(sched) - idx      # 이번 슬롯 포함
                ratio = 1.0 / remaining_slots
                self.logger.info(
                    "장마감 분할 청산 [%d/%d %s] — 잔량의 %.0f%%",
                    idx + 1, len(sched), sched[idx], ratio * 100,
                )
                self._force_close_partial(ratio)

        if is_final and not self._closed_today:
            self._closed_today = True
            self.notifier.send_daily_report(self.journal.get_daily_summary())

            # v3.1: AI 일일 매매 복기 및 학습
            try:
                today_trades = self.journal.get_today_trades()
                ai_review = self.ai_agent.daily_review(today_trades, symbol_names=self.executor.symbol_names)
                if ai_review:
                    self.notifier.send(ai_review)
            except Exception as e:
                self.logger.error("AI 일일 복기 실패: %s", e)

            self.state.save(self.risk)

    def _force_close_partial(self, ratio: float) -> None:
        """잔량의 ratio 비율만 청산 (v2.0)

        Executor 에 force_close_partial(ratio) 이 없으면 건너뛴다.
        이 경우 동작은 v1.1 과 동일하게 force_close 시각 전량 청산이 되므로
        안전하지만, 분할에 따른 슬리피지 개선 효과는 얻지 못한다.
        """
        fn = getattr(self.executor, "force_close_partial", None)
        if callable(fn):
            try:
                fn(ratio)
                return
            except Exception as e:
                self.logger.error(
                    "분할 청산 실패: %s — 최종 슬롯(%s)에서 전량 청산됩니다",
                    e, self.force_close,
                )
                return

        if not self._fc_partial_warned:
            self._fc_partial_warned = True
            self.logger.warning(
                "Executor.force_close_partial(ratio) 미구현 — 분할 청산을 건너뜁니다. "
                "%s 최종 슬롯에서 전량 시장가 청산됩니다.", self.force_close,
            )

    # ── 스크리닝 ──────────────────────────────────────────────
    def _run_screening(self) -> None:
        self.logger.info("종목 스크리닝 시작")
        try:
            candidates = self.screener.screen()
        except Exception as e:
            self.logger.error("스크리닝 실패: %s", e)
            return

        candidate_symbols = [c.symbol for c in candidates]
        current_tracking = list(self.indicators.keys())
        active_positions = list(self.risk.positions.keys())

        # 종목코드 → 종목명 매핑 등록 (텔레그램 알림용)
        for c in candidates:
            if c.name and c.name != c.symbol:
                self.executor.register_symbol_name(c.symbol, c.name)
        
        # 1. 유지할 종목: 보유 중인 포지션이거나, 이번 스크리닝에서 다시 탑에 오른 종목
        to_keep = set(active_positions) | set(candidate_symbols)
        
        # 2. 버릴 종목: 더이상 탑 후보에도 없고 보유 포지션도 없는 종목
        dropped = []
        for sym in current_tracking:
            if sym not in to_keep:
                dropped.append(sym)
                del self.indicators[sym]
                self._orb_finalized.discard(sym)
                
        if dropped:
            self.logger.info("추적 제외(모멘텀 하락): %s", dropped)

        # 3. 신규 종목 추가 검증
        valid: list[str] = []
        for sym in candidate_symbols:
            if sym not in self.indicators:
                self._load_history(sym)
                
            ind = self.indicators.get(sym)
            if ind and ind.set_orb_ready():
                self._orb_finalized.add(sym)
                valid.append(sym)
            else:
                if sym not in active_positions:
                    self.logger.warning("[%s] ORB 미형성 — 추적 제외", sym)
                    if sym in self.indicators:
                        del self.indicators[sym]

        self.logger.info("스크리닝 완료: 유효 추적 종목 %d개", len(self.indicators))
        
        if valid or self.indicators:
            labels = [self.executor._label(s) for s in self.indicators.keys()]
            self.notifier.send(f"🔍 주기적 스크리닝: 추적 {len(self.indicators)}종목\n" + ", ".join(labels))
        else:
            self.notifier.send("🔍 조건 충족 종목 없음 — 대기 중")

    # ── 포지션 관리 ───────────────────────────────────────────
    def _manage_positions(self) -> None:
        is_market_panic = False
        try:
            kosdaq = self.client.get_index_status("1001")
            kospi = self.client.get_index_status("0001")
            if kosdaq.get("change_pct", 0) <= -1.5 and kospi.get("change_pct", 0) <= -1.5:
                is_market_panic = True
        except Exception:
            pass

        for sym in list(self.risk.positions.keys()):
            pos = self.risk.positions.get(sym)
            if not pos:
                continue

            ind = self.indicators.get(sym)
            if ind is None:
                # 복구된 고아 포지션 — 지표가 없으면 즉시 구축
                self.logger.info("[%s] 지표 없음 — 히스토리 로드", sym)
                self._load_history(sym)
                ind = self.indicators.get(sym)
                if ind is None:
                    continue
                ind.set_orb_ready()

            try:
                price = self.client.get_last_price(sym)
                if price > 0:
                    self.executor.process_signal(pos, ind, price, is_market_panic=is_market_panic)
            except Exception as e:
                self.logger.error("[%s] 청산 판정 오류: %s", sym, e)

    # ── 추가매수 탐색 (v2.1) ───────────────────────────────
    def _scan_scale_in(self) -> None:
        """보유 포지션 대상 추가매수 신호 탐색"""
        if not self.risk.scale_in_enabled:
            return

        cash = None
        for sym in list(self.risk.positions.keys()):
            pos = self.risk.positions.get(sym)
            if not pos:
                continue

            if not self.risk.can_add_to_position(sym):
                continue

            ind = self.indicators.get(sym)
            if ind is None:
                continue

            try:
                price = self.client.get_last_price(sym)
                if price <= 0:
                    continue

                ask_total, bid_total = self.client.get_orderbook_totals(sym)

                r_unit = pos.r_unit_pct if pos.r_unit_pct > 0 else (pos.stop_pct or self.strategy.sl_pct)

                signal, size_ratio, reason = self.strategy.should_add(
                    indicators=ind,
                    current_price=float(price),
                    entry_price=pos.entry_price,
                    r_unit_pct=r_unit,
                    add_count=pos.add_count,
                    avg_down_count=pos.avg_down_count,
                    ask_total=ask_total,
                    bid_total=bid_total,
                )

                if signal == ScaleInSignal.NONE:
                    continue

                # 추가매수 수량 계산
                add_qty = max(1, int(pos.initial_qty * size_ratio))

                if cash is None:
                    try:
                        cash = self.client.get_cash()
                    except Exception:
                        cash = 0

                is_avg_down = (signal == ScaleInSignal.AVG_DOWN)
                self.logger.info(
                    "[%s] 추가매수 신호: %s (qty=%d)",
                    sym, reason, add_qty,
                )

                ok = self.executor.scale_in(
                    pos, price, add_qty,
                    reason=reason,
                    is_avg_down=is_avg_down,
                    indicators=ind,
                    available_cash=cash,
                )
                if ok and cash:
                    cash = None  # 추가매수 후 현금 재조회 필요

            except Exception as e:
                self.logger.error("[%s] 추가매수 탐색 오류: %s", sym, e)

    # ── 진입 탐색 ─────────────────────────────────────────────
    def _scan_entries(self) -> None:
        if not self.risk.can_enter():
            return

        # ── v4.0: 시장 지수 흐름 (Market Macro Filter) 검증 ────
        try:
            kosdaq = self.client.get_index_status("1001")
            kospi = self.client.get_index_status("0001")
            if kosdaq.get("change_pct", 0) <= -1.5 and kospi.get("change_pct", 0) <= -1.5:
                self.logger.warning(
                    "[지수 폭락장 차단] 코스피: %+.2f%%, 코스닥: %+.2f%% — 신규 진입 일시 중단",
                    kospi.get("change_pct", 0), kosdaq.get("change_pct", 0)
                )
                return
        except Exception as e:
            self.logger.debug("지수 상태 확인 실패: %s", e)

        cash = None
        try:
            cash = self.client.get_cash()
        except Exception:
            pass

        for sym, ind in list(self.indicators.items()):
            if not self.risk.can_enter(sym):
                continue

            try:
                price = self.client.get_last_price(sym)
                if price <= 0:
                    continue

                ask_total, bid_total = self.client.get_orderbook_totals(sym)
                ok, score = self.strategy.should_enter(
                    ind, float(price), ask_total, bid_total
                )

                self.logger.info("[%s] 눌림목 진입판정: %s @%d (진입=%s)", self.executor._label(sym), score, price, ok)

                if ok:
                    # AI 필터링 (In-Context Learning)
                    recent_trades = self.journal.get_recent_trades(limit=self.ai_agent.context_trades)
                    
                    ai_decision = self.ai_agent.evaluate_entry(
                        symbol=sym,
                        current_price=float(price),
                        score_info=str(score),
                        recent_trades=recent_trades
                    )

                    if not ai_decision["trade"]:
                        self.logger.info("[%s] 진입 취소 (AI 거절): %s", sym, ai_decision["reason"])
                        continue

                    self.logger.info("[%s] AI 최종 승인 (확신도 %d%%): %s", sym, ai_decision["confidence"], ai_decision["reason"])

                    # v3.2: AI 레이턴시 방어 — 가격 재확인 게이트 (Price Re-check Gate)
                    fresh_price = self.client.get_last_price(sym)
                    if fresh_price > 0 and price > 0:
                        slippage_pct = (fresh_price - price) / price * 100
                        if slippage_pct >= 0.3:
                            self.logger.warning(
                                "[%s] 진입 취소 (AI 레이턴시 슬리피지 방지): 신호가 %d -> 현재가 %d (+%.2f%%)",
                                sym, price, fresh_price, slippage_pct
                            )
                            continue
                        price = fresh_price

                    # v2.0: indicators 전달 — 진입 시점 ATR로 손절폭 확정
                    self.executor.enter(
                        sym, price, score.score,
                        available_cash=cash, indicators=ind,
                    )

                    # v3.1: 매수 시점 시장 스냅샷 기록 (AI 학습용)
                    self.ai_agent.capture_market_snapshot(
                        symbol=sym, side="BUY", price=float(price),
                        score_info=str(score), reason=ai_decision["reason"],
                        vwap=ind.vwap.value,
                        ma5=ind.sma_short.value,
                        ma20=ind.sma_long.value,
                        vol_ratio=ind.volume_ratio,
                        dip_pct=score.dip_pct,
                    )

                    if cash:
                        cash = None      # 진입 후 현금 재조회 필요
            except Exception as e:
                self.logger.error("[%s] 진입 탐색 오류: %s", sym, e)

    # ── 종료 ──────────────────────────────────────────────────
    def _shutdown(self) -> None:
        self.logger.info("=" * 56)
        self.logger.info(" 자동매매 종료")

        try:
            self.state.save(self.risk)
        except Exception:
            pass

        summary = self.risk.summary()
        self.logger.info(" 리스크 요약: %s", summary)

        open_positions = list(self.risk.positions.keys())
        if open_positions:
            warn = (
                f"⚠️ 미청산 포지션 {len(open_positions)}개가 남아 있습니다: "
                f"{', '.join(open_positions)}\n"
                f"HTS/MTS에서 직접 확인·청산하세요."
            )
            self.logger.warning(warn)
            self.notifier.send(warn)

        js = self.journal.get_daily_summary()
        if js.get("trades", 0) > 0:
            self.notifier.send_daily_report(js)

            # v3.1: AI 매매 복기 (종료 시에도 학습)
            try:
                today_trades = self.journal.get_today_trades()
                ai_review = self.ai_agent.daily_review(today_trades, symbol_names=self.executor.symbol_names)
                if ai_review:
                    self.notifier.send(ai_review)
            except Exception as e:
                self.logger.error("AI 일일 복기 실패: %s", e)

        self.notifier.send("🛑 자동매매 종료")
        self.logger.info("=" * 56)


# ──────────────────────────────────────────────────────────────
#  백테스트 진입점
# ──────────────────────────────────────────────────────────────
def run_backtest(config: dict, data_dir: str, capital: float,
                 start: str = "", end: str = "") -> None:
    """백테스트 실행

    디렉토리 구조를 자동 판별한다.
      - data/candles/20260728/005930.csv  → 다일자 모드
      - data/candles/005930.csv           → 단일일 모드
    """
    from src.backtest import Backtester, MultiDayBacktester, load_candles_csv
    from src.collector import load_range

    base = Path(data_dir)
    if not base.exists():
        print(f"디렉토리를 찾을 수 없습니다: {data_dir}")
        print("\n데이터를 먼저 수집하세요:")
        print("  python -m src.collector --from-screener")
        return

    # 하위에 날짜 디렉토리가 있으면 다일자 모드
    day_dirs = [d for d in base.iterdir() if d.is_dir() and d.name.isdigit()]

    if day_dirs:
        data = load_range(str(base), start or None, end or None)
        if not data:
            print(f"해당 기간 데이터가 없습니다 ({start or '전체'} ~ {end or '전체'})")
            return

        total_files = sum(len(v) for v in data.values())
        print(f"  {len(data)}영업일 / {total_files}개 파일 로드")
        print(f"  기간: {min(data)} ~ {max(data)}\n")

        mb = MultiDayBacktester(config, initial_capital=capital).run(data)
        print(mb.report())
        return

    # 단일일 모드
    files = sorted(base.glob("*.csv"))
    if not files:
        print(f"CSV 파일이 없습니다: {data_dir}")
        print("포맷: time,open,high,low,close,volume  (파일명 = 종목코드.csv)")
        return

    data_1d = {f.stem: load_candles_csv(f) for f in files}
    for sym, candles in data_1d.items():
        print(f"  {sym}: {len(candles)}봉 로드")

    bt = Backtester(config, initial_capital=capital)
    bt.run(data_1d)
    print(bt.report())


def run_scenarios(config: dict, capital: float, only: str = "") -> int:
    """시나리오 검증 실행"""
    from src.scenario_runner import ScenarioRunner, print_report, print_summary_table

    names = [n.strip() for n in only.split(",")] if only else None
    verdicts = ScenarioRunner(config, capital).run_all(names)
    ok = print_report(verdicts)
    print_summary_table(verdicts)
    return 0 if ok else 1


# ──────────────────────────────────────────────────────────────
#  진입점
# ──────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="KIS 스켈핑 자동매매")
    parser.add_argument("--dry-run",  action="store_true", help="주문 없이 신호만 출력")
    parser.add_argument("--once",     action="store_true", help="1회 루프 후 종료")
    parser.add_argument("--config",   default="config.yaml", help="설정 파일 경로")
    parser.add_argument("--backtest", metavar="DIR", nargs="?", const="data/candles",
                        help="백테스트 실행 (기본 디렉토리: data/candles)")
    parser.add_argument("--scenarios", action="store_true",
                        help="시장 국면별 시나리오 검증 실행")
    parser.add_argument("--only", default="", help="특정 시나리오만 (쉼표 구분)")
    parser.add_argument("--from", dest="start", default="", help="시작일 YYYYMMDD")
    parser.add_argument("--to",   dest="end",   default="", help="종료일 YYYYMMDD")
    parser.add_argument("--capital",  type=float, default=10_000_000, help="초기 자본")
    args = parser.parse_args()

    os.chdir(Path(__file__).parent)
    load_dotenv()

    config = load_config(args.config)

    if args.scenarios:
        setup_logger(level="ERROR")
        sys.exit(run_scenarios(config, args.capital, args.only))

    if args.backtest:
        setup_logger(level="WARNING")
        run_backtest(config, args.backtest, args.capital, args.start, args.end)
        return

    try:
        trader = AutoTrader(config, dry_run=args.dry_run)
    except KeyError as e:
        print(f"\n환경변수 누락: {e}")
        print(".env 파일을 확인하세요 (cp .env.example .env)\n")
        sys.exit(1)

    trader.run(once=args.once)


if __name__ == "__main__":
    main()
