"""
risk_manager.py — 포지션 사이징 & 다층 손실 한도 관리

v2.1 수정사항
  - [신규] 추가매수(피라미딩 + 물타기) 지원
    · Position에 add_count, avg_down_count, last_add_time 필드 추가
    · can_add_to_position() — 추가매수 가능 여부 판정
    · add_to_position() — 포지션 물량 추가 + 평균단가 갱신
    · 추가매수 후 TP1/TP2 리셋 지원 (config: reset_tp)
    · 추가매수 후 평균단가 기준 손절선 재계산 (config: recalc_stop)

v1.1 수정사항
  - [치명] 청산 후 자본이 갱신되지 않아 포지션 사이징이 영구히 고정되던 문제 수정
  - [치명] 분할 청산마다 승/패가 중복 집계되어 승률·연속손실 판정이 왜곡되던 문제 수정
    → 라운드트립(전량 청산) 단위로 1회만 집계
  - 수수료·세금을 반영한 순손익 기준으로 한도 판정
  - 손실 종목 재진입 쿨다운 추가 (동일 종목 반복 손절 방지)
  - 최고가 추적(peak_price)으로 트레일링 스톱 지원
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Optional
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
logger = logging.getLogger("autotrader.risk")


class RiskState(Enum):
    OK             = auto()
    DAILY_HALTED   = auto()
    MONTHLY_HALT   = auto()
    LOSING_STREAK  = auto()
    MAX_TRADES     = auto()
    MAX_CONCURRENT = auto()
    NO_CAPITAL     = auto()


@dataclass
class DailyStats:
    """일별 트레이딩 통계 (라운드트립 기준)"""
    date:        str   = ""
    trades:      int   = 0     # 진입 횟수
    closed:      int   = 0     # 완전 청산된 트레이드 수
    pnl:         float = 0.0   # 순손익 (비용 차감 후)
    gross_pnl:   float = 0.0   # 총손익 (비용 차감 전)
    costs:       float = 0.0   # 수수료 + 세금
    wins:        int   = 0
    losses:      int   = 0
    consec_loss: int   = 0

    @property
    def win_rate(self) -> float:
        return self.wins / self.closed * 100 if self.closed else 0.0


@dataclass
class Position:
    """보유 포지션 정보"""
    symbol:        str
    entry_price:   float
    qty:           int
    entry_time:    str
    score:         int   = 0
    tp1_done:      bool  = False
    tp2_done:      bool  = False
    tp3_armed:     bool  = False
    initial_qty:   int   = 0
    peak_price:    float = 0.0    # 보유 중 최고가 (트레일링용)
    realized_pnl:  float = 0.0    # 부분 청산 누적 실현손익
    realized_cost: float = 0.0    # 부분 청산 누적 비용

    # v2.0 — 진입 시점에 확정되는 포지션별 리스크 단위
    # 매 틱 재계산하면 손절선이 움직이는 버그가 되므로 반드시 고정한다.
    stop_pct:      float = 0.0    # 손절폭 (%). 0이면 config 기본값 사용
    r_unit_pct:    float = 0.0    # 1R 크기 (%). 0이면 stop_pct 로 대체

    # v2.1 — 추가매수(피라미딩/물타기) 추적
    add_count:     int   = 0      # 수익 중 추가매수 횟수
    avg_down_count: int  = 0      # 물타기 횟수
    last_add_time: Optional[datetime] = None  # 마지막 추가매수 시각

    def __post_init__(self) -> None:
        if self.initial_qty == 0:
            self.initial_qty = self.qty
        if self.peak_price == 0.0:
            self.peak_price = self.entry_price

    def update_peak(self, price: float) -> None:
        if price > self.peak_price:
            self.peak_price = price

    def current_pnl_pct(self, current_price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (current_price - self.entry_price) / self.entry_price * 100

    def current_pnl_krw(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.qty


class RiskManager:
    """포지션 사이징 & 리스크 한도 관리"""

    def __init__(self, config: dict, initial_capital: float) -> None:
        risk_cfg = config.get("risk", {})

        self.risk_per_trade_pct:     float = float(risk_cfg.get("risk_per_trade_pct",     1.5))
        self.max_position_pct:       float = float(risk_cfg.get("max_position_pct",      50.0))
        self.max_concurrent:         int   = int(risk_cfg.get("max_concurrent",            2))
        self.max_daily_trades:       int   = int(risk_cfg.get("max_daily_trades",          8))
        self.daily_loss_limit_pct:   float = float(risk_cfg.get("daily_loss_limit_pct",   3.0))
        self.monthly_loss_limit_pct: float = float(risk_cfg.get("monthly_loss_limit_pct",10.0))
        self.consec_loss_limit:      int   = int(risk_cfg.get("consecutive_loss_limit",    3))
        self.reentry_cooldown_min:   int   = int(risk_cfg.get("reentry_cooldown_min",     30))
        self.max_trades_per_symbol:  int   = int(risk_cfg.get("max_trades_per_symbol",     2))
        self.min_order_amount:       float = float(risk_cfg.get("min_order_amount",   100_000))

        self.sl_pct: float = float(config.get("stop_loss", {}).get("sl_pct", 1.0))

        # ── 추가매수 설정 (v2.1) ──────────────────────────────
        si_cfg = config.get("scale_in", {})
        self.scale_in_enabled:      bool  = bool(si_cfg.get("enabled", False))
        self.scale_in_max_adds:     int   = int(si_cfg.get("max_adds", 2))
        self.scale_in_min_interval: int   = int(si_cfg.get("min_interval_sec", 180))
        self.scale_in_recalc_stop:  bool  = bool(si_cfg.get("recalc_stop", True))
        self.scale_in_reset_tp:     bool  = bool(si_cfg.get("reset_tp", True))
        # 물타기
        self.avg_down_enabled:      bool  = bool(si_cfg.get("avg_down_enabled", False))
        self.avg_down_max:          int   = int(si_cfg.get("avg_down_max", 1))

        # 자본 — 일일 한도는 '장 시작 자본' 기준으로 고정 판정
        self.capital:      float = initial_capital
        self.day_start_capital: float = initial_capital
        self.month_start_capital: float = initial_capital
        self.month_pnl:    float = 0.0

        today = datetime.now(tz=KST).strftime("%Y%m%d")
        self.daily: DailyStats = DailyStats(date=today)
        self.positions: dict[str, Position] = {}

        self._cooldown: dict[str, datetime] = {}      # 종목별 재진입 금지 해제 시각
        self._symbol_trades: dict[str, int] = {}      # 종목별 당일 거래 횟수

    # ── 상태 검사 ─────────────────────────────────────────────
    def check_state(self) -> RiskState:
        if self.capital <= 0:
            return RiskState.NO_CAPITAL

        if self.month_pnl <= -(self.month_start_capital * self.monthly_loss_limit_pct / 100):
            return RiskState.MONTHLY_HALT

        if self.daily.pnl <= -(self.day_start_capital * self.daily_loss_limit_pct / 100):
            return RiskState.DAILY_HALTED

        if self.daily.consec_loss >= self.consec_loss_limit:
            return RiskState.LOSING_STREAK

        if self.daily.trades >= self.max_daily_trades:
            return RiskState.MAX_TRADES

        if len(self.positions) >= self.max_concurrent:
            return RiskState.MAX_CONCURRENT

        return RiskState.OK

    def can_enter(self, symbol: Optional[str] = None) -> bool:
        """신규 진입 가능 여부 (종목 지정 시 쿨다운·종목별 한도까지 검사)

        이미 보유 중인 종목은 신규 진입 불가. 추가매수는 can_add_to_position() 참조.
        """
        state = self.check_state()
        if state != RiskState.OK:
            logger.info("진입 불가 — %s", state.name)
            return False

        if symbol is None:
            return True

        if symbol in self.positions:
            return False

        # 재진입 쿨다운
        until = self._cooldown.get(symbol)
        if until and datetime.now(tz=KST) < until:
            remain = (until - datetime.now(tz=KST)).seconds // 60
            logger.info("[%s] 재진입 쿨다운 %d분 남음", symbol, remain)
            return False

        # 종목별 당일 거래 한도
        if self._symbol_trades.get(symbol, 0) >= self.max_trades_per_symbol:
            logger.info("[%s] 종목별 거래 한도 도달", symbol)
            return False

        return True

    # ── 추가매수 판정 (v2.1) ──────────────────────────────────
    def can_add_to_position(self, symbol: str) -> bool:
        """추가매수 가능 여부

        기본 리스크 한도(일일손실·연속손실·거래횟수)는 그대로 적용하되,
        동시보유 한도(MAX_CONCURRENT)는 이미 보유 중이므로 체크하지 않는다.
        """
        if not self.scale_in_enabled:
            return False

        pos = self.positions.get(symbol)
        if pos is None:
            return False

        # 기본 리스크 상태 (MAX_CONCURRENT 제외)
        state = self.check_state()
        if state not in (RiskState.OK, RiskState.MAX_CONCURRENT):
            logger.info("[%s] 추가매수 불가 — %s", symbol, state.name)
            return False

        # 총 추가매수 횟수 한도
        total_adds = pos.add_count + pos.avg_down_count
        if total_adds >= self.scale_in_max_adds:
            return False

        # 최소 간격
        if pos.last_add_time:
            elapsed = (datetime.now(tz=KST) - pos.last_add_time).total_seconds()
            if elapsed < self.scale_in_min_interval:
                return False

        # 총 포지션 금액 한도
        pos_value = pos.entry_price * pos.qty
        if pos_value >= self.capital * self.max_position_pct / 100:
            logger.info("[%s] 추가매수 불가 — 포지션 상한", symbol)
            return False

        return True

    def add_to_position(
        self,
        symbol: str,
        add_price: float,
        add_qty: int,
        cost: float = 0.0,
        is_avg_down: bool = False,
        new_stop_pct: float = 0.0,
        new_r_unit_pct: float = 0.0,
    ) -> bool:
        """포지션에 물량 추가 + 평균단가 갱신

        Args:
            is_avg_down: True면 물타기 카운트 증가, False면 피라미딩 카운트 증가
            new_stop_pct:   재계산된 손절폭 (recalc_stop이면 호출자가 계산해 전달)
            new_r_unit_pct: 재계산된 1R (recalc_stop이면 호출자가 계산해 전달)

        Returns:
            성공 여부
        """
        pos = self.positions.get(symbol)
        if pos is None or add_qty <= 0:
            return False

        # 평균단가 갱신
        old_total = pos.entry_price * pos.qty
        new_total = add_price * add_qty
        new_qty = pos.qty + add_qty
        pos.entry_price = (old_total + new_total) / new_qty
        pos.qty = new_qty

        # 추가매수 카운트
        if is_avg_down:
            pos.avg_down_count += 1
        else:
            pos.add_count += 1
        pos.last_add_time = datetime.now(tz=KST)

        # TP 리셋 (설정 시)
        if self.scale_in_reset_tp:
            pos.tp1_done = False
            pos.tp2_done = False
            pos.tp3_armed = False
            pos.initial_qty = new_qty  # 익절 수량 기준도 갱신
            pos.peak_price = max(pos.peak_price, add_price)
            logger.info("[%s] TP 리셋 (initial_qty=%d)", symbol, new_qty)

        # 손절선 재계산 (설정 시)
        if self.scale_in_recalc_stop and new_stop_pct > 0:
            pos.stop_pct = new_stop_pct
            pos.r_unit_pct = new_r_unit_pct if new_r_unit_pct > 0 else new_stop_pct
            logger.info(
                "[%s] 손절선 재계산: 평단=%.0f → stop=%.2f%% (1R=%.2f%%)",
                symbol, pos.entry_price, pos.stop_pct, pos.r_unit_pct,
            )

        # 매수 수수료 반영
        if cost:
            pos.realized_cost += cost
            self.daily.costs += cost
            self.capital -= cost

        label = "물타기" if is_avg_down else "추가매수"
        logger.info(
            "%s: %s +%d주 @%.0f → 총 %d주 평단 %.0f (피라미딩%d/물타기%d)",
            label, symbol, add_qty, add_price,
            pos.qty, pos.entry_price,
            pos.add_count, pos.avg_down_count,
        )
        return True

    # ── 포지션 사이징 ─────────────────────────────────────────
    def calc_position_size(self, entry_price: float, available_cash: Optional[float] = None,
                           stop_pct: Optional[float] = None) -> int:
        """리스크 기준 포지션 사이징 (주식 수)

        수량 = min(
            리스크금액 / 주당손절폭,
            자본 × 최대포지션비율 / 진입가,
            가용현금 / 진입가
        )

        v2.0: stop_pct 를 주면 종목별 ATR 손절폭으로 사이징한다.
              손절폭이 넓은(변동성 큰) 종목은 자동으로 수량이 줄어
              거래당 리스크 금액이 일정하게 유지된다.
        """
        if entry_price <= 0 or self.capital <= 0:
            return 0

        width = stop_pct if (stop_pct and stop_pct > 0) else self.sl_pct
        risk_amount   = self.capital * self.risk_per_trade_pct / 100
        sl_per_share  = entry_price * width / 100
        if sl_per_share <= 0:
            return 0

        size_by_risk = int(risk_amount / sl_per_share)
        size_by_cap  = int(self.capital * self.max_position_pct / 100 / entry_price)
        size = min(size_by_risk, size_by_cap)

        # 실제 가용 현금 한도 (미수 방지)
        if available_cash is not None and available_cash > 0:
            size = min(size, int(available_cash * 0.98 / entry_price))

        # 최소 주문 금액 미달 시 진입 포기
        if size * entry_price < self.min_order_amount:
            logger.info(
                "최소 주문금액 미달: %d주 × %.0f = %.0f원 < %.0f원",
                size, entry_price, size * entry_price, self.min_order_amount,
            )
            return 0

        logger.info(
            "포지션 사이징: 자본=%.0f → %d주 (리스크기준=%d, 자본상한=%d)",
            self.capital, size, size_by_risk, size_by_cap,
        )
        return max(0, size)

    # ── 포지션 등록 ───────────────────────────────────────────
    def open_position(self, position: Position, cost: float = 0.0) -> None:
        self.positions[position.symbol] = position
        self.daily.trades += 1
        self._symbol_trades[position.symbol] = self._symbol_trades.get(position.symbol, 0) + 1

        # 매수 수수료 즉시 반영
        if cost:
            position.realized_cost += cost
            self.daily.costs += cost
            self.capital -= cost

        logger.info(
            "포지션 오픈: %s %d주 @%.0f (당일 %d/%d회)",
            position.symbol, position.qty, position.entry_price,
            self.daily.trades, self.max_daily_trades,
        )

    # ── 포지션 청산 ───────────────────────────────────────────
    def close_position(
        self,
        symbol: str,
        exit_price: float,
        qty: Optional[int] = None,
        cost: float = 0.0,
    ) -> float:
        """포지션 (부분) 청산 후 순손익 반환

        승/패 집계는 포지션이 완전히 소멸할 때 라운드트립 단위로 1회만 수행한다.
        """
        pos = self.positions.get(symbol)
        if not pos:
            logger.warning("청산 대상 없음: %s", symbol)
            return 0.0

        close_qty = min(qty if qty else pos.qty, pos.qty)
        if close_qty <= 0:
            return 0.0

        gross = (exit_price - pos.entry_price) * close_qty
        net   = gross - cost

        pos.qty           -= close_qty
        pos.realized_pnl  += net
        pos.realized_cost += cost

        # 통계 — 손익은 즉시 반영
        self.daily.gross_pnl += gross
        self.daily.pnl       += net
        self.daily.costs     += cost
        self.month_pnl       += net
        self.capital         += net          # ★ 자본 실시간 갱신

        fully_closed = pos.qty <= 0
        if fully_closed:
            trade_pnl = pos.realized_pnl
            self.daily.closed += 1
            if trade_pnl >= 0:
                self.daily.wins += 1
                self.daily.consec_loss = 0
            else:
                self.daily.losses += 1
                self.daily.consec_loss += 1
                # 손실 마감 종목은 쿨다운 적용
                self._cooldown[symbol] = (
                    datetime.now(tz=KST) + timedelta(minutes=self.reentry_cooldown_min)
                )
            del self.positions[symbol]

            logger.info(
                "라운드트립 종료: %s 순손익=%.0f원 | 당일 %d승 %d패 (승률 %.1f%%)",
                symbol, trade_pnl, self.daily.wins, self.daily.losses, self.daily.win_rate,
            )

        logger.info(
            "청산: %s %d주 @%.0f → 순손익=%.0f원 (잔량 %d주, 당일누적 %.0f원)",
            symbol, close_qty, exit_price, net, pos.qty, self.daily.pnl,
        )
        return net

    # ── 자본 / 초기화 ─────────────────────────────────────────
    def sync_capital(self, broker_capital: float) -> None:
        """브로커 실제 잔고와 동기화 (드리프트 보정)"""
        if broker_capital <= 0:
            return
        drift = broker_capital - self.capital
        if abs(drift) > max(1000.0, self.capital * 0.01):
            logger.warning(
                "자본 드리프트 보정: 내부=%.0f → 브로커=%.0f (차이 %.0f)",
                self.capital, broker_capital, drift,
            )
        self.capital = broker_capital

    def reset_daily(self) -> None:
        """일일 통계 초기화 (매 영업일 장 시작 시 1회)"""
        today = datetime.now(tz=KST).strftime("%Y%m%d")
        self.daily = DailyStats(date=today)
        self.day_start_capital = self.capital
        self._cooldown.clear()
        self._symbol_trades.clear()
        logger.info("일일 통계 초기화: %s (시작자본 %.0f원)", today, self.capital)

    def reset_monthly(self) -> None:
        self.month_pnl = 0.0
        self.month_start_capital = self.capital
        logger.info("월간 통계 초기화 (시작자본 %.0f원)", self.capital)

    # ── 상태 요약 ─────────────────────────────────────────────
    def summary(self) -> dict:
        return {
            "capital":      round(self.capital),
            "daily_pnl":    round(self.daily.pnl),
            "daily_costs":  round(self.daily.costs),
            "daily_trades": self.daily.trades,
            "closed":       self.daily.closed,
            "wins":         self.daily.wins,
            "losses":       self.daily.losses,
            "win_rate":     round(self.daily.win_rate, 1),
            "month_pnl":    round(self.month_pnl),
            "positions":    len(self.positions),
            "consec_loss":  self.daily.consec_loss,
            "state":        self.check_state().name,
        }
