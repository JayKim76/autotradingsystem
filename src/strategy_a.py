"""
strategy_a.py — 전략 A: 240일선 돌파 & 거래량 급증 (전략A설계.md 동결본)

출처: 이상우 《주식차트 절대비기 300선!》 전략 A + 손절·청산 규칙.
진입/사이징/비용의 공통 골격은 breakout.py와 동일 규율(신호=종가 확정,
체결=익일 시가)을 유지하고, 진입 조건과 청산 규칙만 책의 것으로 교체했다.

진입 (신호일 종가):
  - 전일 종가 ≤ 전일 240일선, 당일 종가 > 당일 240일선 (상향 돌파)
  - 당일 거래량 ≥ 직전 20일 평균 거래량(당일 제외)의 5배
  - 양봉 + 윗꼬리 ≤ 몸통의 50%
청산:
  - 절대 손절 -7% (장중, 체결가 기준)
  - 종가가 돌파봉 몸통 중심가 하회 마감 → 익일 시가 (돌파실패)
  - 60일 고점 95% 이상에서 유성형 또는 거래량 3배 → 익일 시가 (천정청산)
  - 시간 청산 없음
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .backtest import BTTrade
from .breakout import BreakoutBacktester
from .swing import DailyBar, sma

logger = logging.getLogger("autotrader.strategy_a")


def prior_mean(values: list[float], period: int) -> list[Optional[float]]:
    """i번째 값 = i를 제외한 직전 period개 평균 (당일 급증 판정용, 미래참조 없음)"""
    out: list[Optional[float]] = [None] * len(values)
    for i in range(period, len(values)):
        out[i] = sum(values[i - period:i]) / period
    return out


def rolling_max(values: list[float], period: int) -> list[Optional[float]]:
    """i번째 값 = i를 포함한 직전 period개 최대값 (초반은 가능한 만큼만)"""
    out: list[Optional[float]] = [None] * len(values)
    for i in range(len(values)):
        start = max(0, i - period + 1)
        out[i] = max(values[start:i + 1])
    return out


@dataclass
class StrategyAConfig:
    ma_period: int = 240
    volume_mult: float = 5.0
    avg_volume_days: int = 20
    upper_wick_max_ratio: float = 0.5
    stop_pct: float = 7.0
    high_lookback_days: int = 60
    near_high_ratio: float = 0.95
    climax_volume_mult: float = 3.0
    shooting_star_wick_ratio: float = 2.0
    risk_pct: float = 0.75
    max_position_pct: float = 25.0
    max_positions: int = 4
    max_entries_per_day: int = 2
    min_avg_value_krw: float = 5_000_000_000
    gap_up_limit_pct: float = 2.0
    gap_down_limit_pct: float = 5.0
    regime_symbol: str = "069500"

    @classmethod
    def from_config(cls, config: dict) -> "StrategyAConfig":
        section = (config or {}).get("strategy_a", {}) or {}
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in section.items() if k in known})


@dataclass
class _PosA:
    symbol: str
    entry_date: str
    entry_price: float
    qty: int
    stop_price: float
    structure_stop: float
    entry_cost: float
    entry_index: int


@dataclass
class _SeriesA:
    bars: list[DailyBar]
    index: dict[str, int]
    closes: list[float]
    ma_long: list[Optional[float]]
    avg_vol: list[Optional[float]]       # 당일 제외 직전 20일 평균 거래량
    avg_value: list[Optional[float]]
    high_lookback: list[Optional[float]]
    ma_fast: list[Optional[float]]       # 레짐 판정용


class StrategyABacktester(BreakoutBacktester):
    """전략 A 백테스터. 비용·체결 헬퍼는 BreakoutBacktester 것을 상속한다."""

    def __init__(self, bars_by_symbol: dict[str, list[DailyBar]],
                 cfg: StrategyAConfig, base_config: dict):
        # BreakoutBacktester.__init__은 돈치안 시리즈를 만들므로 우회한다
        cost = (base_config or {}).get("cost", {}) or {}
        self.commission_pct = float(cost.get("commission_pct", 0.015))
        self.tax_pct = float(cost.get("tax_pct", 0.20))
        execution = (base_config or {}).get("execution", {}) or {}
        self.slippage_bps = int(execution.get("slippage_bps", 5))
        self.cfg = cfg  # type: ignore[assignment]
        self.a_cfg = cfg

        self.series_a: dict[str, _SeriesA] = {}
        for symbol, bars in bars_by_symbol.items():
            if not bars:
                continue
            closes = [b.close for b in bars]
            volumes = [float(b.volume) for b in bars]
            values = [b.value_krw for b in bars]
            self.series_a[symbol] = _SeriesA(
                bars=bars,
                index={b.date: i for i, b in enumerate(bars)},
                closes=closes,
                ma_long=sma(closes, cfg.ma_period),
                avg_vol=prior_mean(volumes, cfg.avg_volume_days),
                avg_value=sma(values, 20),
                high_lookback=rolling_max([b.high for b in bars], cfg.high_lookback_days),
                ma_fast=sma(closes, 20),
            )
        self.all_dates = sorted({b.date for bars in bars_by_symbol.values() for b in bars})

    # ── 레짐 (지수 필터 — 공통 골격) ─────────────────────────────
    def _regime_ok(self, date: str) -> bool:
        s = self.series_a.get(self.a_cfg.regime_symbol)
        if s is None:
            return True
        i = s.index.get(date)
        if i is None or s.ma_fast[i] is None:
            return True
        return s.closes[i] > s.ma_fast[i]

    # ── 진입 신호 ────────────────────────────────────────────────
    def _entry_signal(self, symbol: str, date: str) -> Optional[dict]:
        cfg = self.a_cfg
        s = self.series_a[symbol]
        i = s.index.get(date)
        if i is None or i < 1:
            return None
        ma_now, ma_prev = s.ma_long[i], s.ma_long[i - 1]
        if ma_now is None or ma_prev is None:
            return None
        if not (s.closes[i - 1] <= ma_prev and s.closes[i] > ma_now):
            return None                                    # 240일선 상향 돌파 아님
        avg_vol = s.avg_vol[i]
        if avg_vol is None or avg_vol <= 0:
            return None
        volume_ratio = s.bars[i].volume / avg_vol
        if volume_ratio < cfg.volume_mult:
            return None                                    # 거래량 급증 부족
        bar = s.bars[i]
        body = bar.close - bar.open
        if body <= 0:
            return None                                    # 양봉 아님
        if (bar.high - bar.close) > body * cfg.upper_wick_max_ratio:
            return None                                    # 윗꼬리 과다
        if s.avg_value[i] is None or s.avg_value[i] < cfg.min_avg_value_krw:
            return None                                    # 유동성 부족
        return {
            "prev_close": s.closes[i],
            "volume_ratio": volume_ratio,
            "structure_stop": (bar.open + bar.close) / 2,  # 돌파봉 몸통 중심가
        }

    # ── 종가 청산 판정 (돌파실패 / 천정청산) ─────────────────────
    def _close_exit_reason(self, pos: _PosA, date: str) -> Optional[str]:
        cfg = self.a_cfg
        s = self.series_a[pos.symbol]
        j = s.index.get(date)
        if j is None:
            return None
        bar = s.bars[j]
        if bar.close < pos.structure_stop:
            return "돌파실패"
        high = s.high_lookback[j]
        if high and bar.close >= high * cfg.near_high_ratio:
            body = abs(bar.close - bar.open)
            upper_wick = bar.high - max(bar.open, bar.close)
            shooting_star = (bar.close < bar.open and body > 0
                             and upper_wick >= body * cfg.shooting_star_wick_ratio)
            avg_vol = s.avg_vol[j]
            climax = (avg_vol is not None and avg_vol > 0
                      and bar.volume >= avg_vol * cfg.climax_volume_mult)
            if shooting_star or climax:
                return "천정청산"
        return None

    def run(self, capital: float = 10_000_000,
            window: Optional[tuple[str, str]] = None) -> dict:
        cfg = self.a_cfg
        dates = self.all_dates
        if window:
            dates = [d for d in dates if window[0] <= d <= window[1]]
        if not dates:
            return {"trades": [], "equity": [], "end_capital": capital}

        date_pos = {d: gi for gi, d in enumerate(self.all_dates)}
        cash = capital
        positions: dict[str, _PosA] = {}
        pending_exit: dict[str, str] = {}
        trades: list[BTTrade] = []
        equity: list[tuple[str, float]] = []

        def close_position(pos: _PosA, date: str, raw_price: float, reason: str) -> None:
            nonlocal cash
            fill = self._sell_fill(raw_price)
            sell_cost = self._sell_cost(fill, pos.qty)
            gross = (fill - pos.entry_price) * pos.qty
            cost = pos.entry_cost + sell_cost
            cash += fill * pos.qty - sell_cost
            trades.append(BTTrade(
                symbol=pos.symbol, entry_time=pos.entry_date, exit_time=date,
                entry_price=pos.entry_price, exit_price=fill, qty=pos.qty,
                reason=reason, gross=gross, cost=cost,
                date=date, r_unit_pct=cfg.stop_pct,
            ))
            del positions[pos.symbol]
            pending_exit.pop(pos.symbol, None)

        for di, date in enumerate(dates):
            gi = date_pos[date]
            is_last = di == len(dates) - 1

            # 1) 예약 청산 (돌파실패/천정)을 시가에 실행
            for symbol, reason in list(pending_exit.items()):
                if symbol not in positions:
                    pending_exit.pop(symbol, None)
                    continue
                s = self.series_a[symbol]
                j = s.index.get(date)
                if j is not None:
                    close_position(positions[symbol], date, s.bars[j].open, reason)

            # 2) 절대 손절 -7% (장중)
            for symbol, pos in list(positions.items()):
                s = self.series_a[symbol]
                j = s.index.get(date)
                if j is None:
                    continue
                bar = s.bars[j]
                if bar.open <= pos.stop_price:
                    close_position(pos, date, bar.open, "손절")
                elif bar.low <= pos.stop_price:
                    close_position(pos, date, pos.stop_price, "손절")

            # 3) 신규 진입 (전일 종가 신호 → 오늘 시가)
            if di > 0 and not is_last:
                prev_date = dates[di - 1]
                if len(positions) < cfg.max_positions and self._regime_ok(prev_date):
                    equity_now = cash
                    for p in positions.values():
                        ps = self.series_a[p.symbol]
                        pj = ps.index.get(prev_date)
                        equity_now += p.qty * (ps.closes[pj] if pj is not None else p.entry_price)
                    signals = []
                    for symbol in self.series_a:
                        if symbol == cfg.regime_symbol or symbol in positions:
                            continue
                        sig = self._entry_signal(symbol, prev_date)
                        if sig:
                            signals.append((sig["volume_ratio"], symbol, sig))
                    signals.sort(key=lambda x: -x[0])   # 거래량 증가율 큰 순
                    entered = 0
                    for _vr, symbol, sig in signals:
                        if entered >= cfg.max_entries_per_day:
                            break
                        if len(positions) >= cfg.max_positions:
                            break
                        s = self.series_a[symbol]
                        j = s.index.get(date)
                        if j is None:
                            continue
                        bar = s.bars[j]
                        if bar.open > sig["prev_close"] * (1 + cfg.gap_up_limit_pct / 100):
                            continue
                        if bar.open < sig["prev_close"] * (1 - cfg.gap_down_limit_pct / 100):
                            continue
                        fill = self._buy_fill(bar.open)
                        stop = fill * (1 - cfg.stop_pct / 100)
                        risk_per_share = fill - stop
                        if risk_per_share <= 0:
                            continue
                        risk_budget = equity_now * cfg.risk_pct / 100
                        qty = int(risk_budget // risk_per_share)
                        cap_by_weight = int((equity_now * cfg.max_position_pct / 100) // fill)
                        qty = min(qty, cap_by_weight)
                        qty = min(qty, int(cash // fill))
                        if qty <= 0:
                            continue
                        buy_cost = self._buy_cost(fill, qty)
                        cash -= fill * qty + buy_cost
                        positions[symbol] = _PosA(
                            symbol=symbol, entry_date=date, entry_price=fill, qty=qty,
                            stop_price=stop, structure_stop=sig["structure_stop"],
                            entry_cost=buy_cost, entry_index=gi,
                        )
                        entered += 1

            # 4) 종가 판정: 돌파실패/천정 → 내일 시가 청산 예약. 마지막 날은 강제청산
            for symbol, pos in list(positions.items()):
                if is_last:
                    s = self.series_a[symbol]
                    j = s.index.get(date)
                    if j is not None:
                        close_position(pos, date, s.bars[j].close, "기간종료")
                    continue
                reason = self._close_exit_reason(pos, date)
                if reason:
                    pending_exit[symbol] = reason

            # 5) 평가금액 기록 (종가 기준)
            total = cash
            for p in positions.values():
                ps = self.series_a[p.symbol]
                pj = ps.index.get(date)
                if pj is not None:
                    total += p.qty * ps.closes[pj]
            equity.append((date, total))

        return {"trades": trades, "equity": equity, "end_capital": cash}
