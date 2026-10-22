"""
breakout.py — 일봉 돌파형 스윙 전략 (돈치안 채널 돌파, 돌파전략설계.md 참조)

눌림목과의 차이:
  - 진입: 눌림을 기다리지 않고 20일 신고가 돌파 + 거래대금 급증에서 들어간다
  - 청산: 추세 청산선이 진입가에서 멀다 (10일 최저가 채널). 승자를 일찍 자르지 않는다
리스크 골격(사이징·손절·갭 규칙·비용 모델)은 swing.py와 동일하게 유지한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .backtest import BTTrade
from .swing import DailyBar, atr_series, sma

logger = logging.getLogger("autotrader.breakout")


def donchian_high(bars: list[DailyBar], period: int) -> list[Optional[float]]:
    """i번째 값 = 오늘을 제외한 직전 period일 최고가 (돌파 판정용)"""
    out: list[Optional[float]] = [None] * len(bars)
    for i in range(period, len(bars)):
        out[i] = max(b.high for b in bars[i - period:i])
    return out


def donchian_low(bars: list[DailyBar], period: int) -> list[Optional[float]]:
    """i번째 값 = 오늘을 제외한 직전 period일 최저가 (채널 청산용)"""
    out: list[Optional[float]] = [None] * len(bars)
    for i in range(period, len(bars)):
        out[i] = min(b.low for b in bars[i - period:i])
    return out


@dataclass
class BreakoutConfig:
    breakout_days: int = 20
    exit_channel_days: int = 10
    volume_mult: float = 1.5
    atr_period: int = 14
    stop_atr_mult: float = 2.0
    max_hold_days: int = 20
    risk_pct: float = 0.75
    max_position_pct: float = 25.0
    max_positions: int = 4
    max_entries_per_day: int = 2
    min_avg_value_krw: float = 5_000_000_000
    gap_up_limit_pct: float = 2.0
    gap_down_limit_pct: float = 5.0
    regime_symbol: str = "069500"

    @classmethod
    def from_config(cls, config: dict) -> "BreakoutConfig":
        section = (config or {}).get("breakout", {}) or {}
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in section.items() if k in known})


@dataclass
class _Pos:
    symbol: str
    entry_date: str
    entry_price: float
    qty: int
    stop_price: float
    r_unit_pct: float
    entry_cost: float
    entry_index: int


@dataclass
class _Series:
    bars: list[DailyBar]
    index: dict[str, int]
    closes: list[float]
    dc_high: list[Optional[float]]
    dc_low: list[Optional[float]]
    atr: list[Optional[float]]
    avg_value: list[Optional[float]]
    ma_fast: list[Optional[float]]     # 레짐 판정용 (regime 심볼)


class BreakoutBacktester:
    """돌파형 백테스터. 신호=종가 확정, 체결=익일 시가 (swing과 동일 규율)."""

    def __init__(self, bars_by_symbol: dict[str, list[DailyBar]],
                 cfg: BreakoutConfig, base_config: dict):
        self.cfg = cfg
        cost = (base_config or {}).get("cost", {}) or {}
        self.commission_pct = float(cost.get("commission_pct", 0.015))
        self.tax_pct = float(cost.get("tax_pct", 0.20))
        execution = (base_config or {}).get("execution", {}) or {}
        self.slippage_bps = int(execution.get("slippage_bps", 5))

        self.series: dict[str, _Series] = {}
        for symbol, bars in bars_by_symbol.items():
            if not bars:
                continue
            closes = [b.close for b in bars]
            values = [b.value_krw for b in bars]
            self.series[symbol] = _Series(
                bars=bars,
                index={b.date: i for i, b in enumerate(bars)},
                closes=closes,
                dc_high=donchian_high(bars, cfg.breakout_days),
                dc_low=donchian_low(bars, cfg.exit_channel_days),
                atr=atr_series(bars, cfg.atr_period),
                avg_value=sma(values, 20),
                ma_fast=sma(closes, 20),
            )
        self.all_dates = sorted({b.date for bars in bars_by_symbol.values() for b in bars})

    def _buy_fill(self, price: float) -> float:
        return price * (1 + self.slippage_bps / 10000)

    def _sell_fill(self, price: float) -> float:
        return price * (1 - self.slippage_bps / 10000)

    def _buy_cost(self, fill: float, qty: int) -> float:
        return fill * qty * self.commission_pct / 100

    def _sell_cost(self, fill: float, qty: int) -> float:
        return fill * qty * (self.commission_pct + self.tax_pct) / 100

    def _regime_ok(self, date: str) -> bool:
        s = self.series.get(self.cfg.regime_symbol)
        if s is None:
            return True
        i = s.index.get(date)
        if i is None or s.ma_fast[i] is None:
            return True
        return s.closes[i] > s.ma_fast[i]

    def _entry_signal(self, symbol: str, date: str) -> Optional[dict]:
        s = self.series[symbol]
        i = s.index.get(date)
        if i is None or i < 1:
            return None
        dc = s.dc_high[i]
        atr = s.atr[i]
        if dc is None or atr is None:
            return None
        if s.closes[i] <= dc:                       # 돌파 아님
            return None
        if s.avg_value[i] is None or s.avg_value[i] < self.cfg.min_avg_value_krw:
            return None
        value_ratio = s.bars[i].value_krw / s.avg_value[i] if s.avg_value[i] else 0.0
        if value_ratio < self.cfg.volume_mult:     # 거래 없는 돌파 제외
            return None
        return {"atr": atr, "value_ratio": value_ratio, "prev_close": s.closes[i]}

    def run(self, capital: float = 10_000_000,
            window: Optional[tuple[str, str]] = None) -> dict:
        cfg = self.cfg
        dates = self.all_dates
        if window:
            dates = [d for d in dates if window[0] <= d <= window[1]]
        if not dates:
            return {"trades": [], "equity": [], "end_capital": capital}

        date_pos = {d: gi for gi, d in enumerate(self.all_dates)}
        cash = capital
        positions: dict[str, _Pos] = {}
        pending_exit: dict[str, str] = {}
        trades: list[BTTrade] = []
        equity: list[tuple[str, float]] = []

        def close_position(pos: _Pos, date: str, raw_price: float, reason: str) -> None:
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
                date=date, r_unit_pct=pos.r_unit_pct,
            ))
            del positions[pos.symbol]
            pending_exit.pop(pos.symbol, None)

        for di, date in enumerate(dates):
            gi = date_pos[date]
            is_last = di == len(dates) - 1

            # 1) 예약 청산 (채널이탈/시간)을 시가에 실행
            for symbol, reason in list(pending_exit.items()):
                if symbol not in positions:
                    pending_exit.pop(symbol, None)
                    continue
                s = self.series[symbol]
                j = s.index.get(date)
                if j is not None:
                    close_position(positions[symbol], date, s.bars[j].open, reason)

            # 2) 손절 (장중) — 갭하락이면 시가, 아니면 손절가
            for symbol, pos in list(positions.items()):
                s = self.series[symbol]
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
                        ps = self.series[p.symbol]
                        pj = ps.index.get(prev_date)
                        equity_now += p.qty * (ps.closes[pj] if pj is not None else p.entry_price)
                    signals = []
                    for symbol in self.series:
                        if symbol == cfg.regime_symbol or symbol in positions:
                            continue
                        sig = self._entry_signal(symbol, prev_date)
                        if sig:
                            signals.append((sig["value_ratio"], symbol, sig))
                    signals.sort(key=lambda x: -x[0])   # 거래대금 증가율 큰 순
                    entered = 0
                    for _vr, symbol, sig in signals:
                        if entered >= cfg.max_entries_per_day:
                            break
                        if len(positions) >= cfg.max_positions:
                            break
                        s = self.series[symbol]
                        j = s.index.get(date)
                        if j is None:
                            continue
                        bar = s.bars[j]
                        if bar.open > sig["prev_close"] * (1 + cfg.gap_up_limit_pct / 100):
                            continue
                        if bar.open < sig["prev_close"] * (1 - cfg.gap_down_limit_pct / 100):
                            continue
                        fill = self._buy_fill(bar.open)
                        stop = bar.open - cfg.stop_atr_mult * sig["atr"]
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
                        positions[symbol] = _Pos(
                            symbol=symbol, entry_date=date, entry_price=fill, qty=qty,
                            stop_price=stop,
                            r_unit_pct=(fill - stop) / fill * 100,
                            entry_cost=buy_cost, entry_index=gi,
                        )
                        entered += 1

            # 4) 종가 판정: 채널이탈/시간 → 내일 시가 청산 예약. 마지막 날은 종가 강제청산
            for symbol, pos in list(positions.items()):
                s = self.series[symbol]
                j = s.index.get(date)
                if j is None:
                    continue
                if is_last:
                    close_position(pos, date, s.bars[j].close, "기간종료")
                    continue
                held = gi - pos.entry_index
                channel_break = (s.dc_low[j] is not None and s.closes[j] < s.dc_low[j])
                if channel_break:
                    pending_exit[symbol] = "채널이탈"
                elif held >= cfg.max_hold_days:
                    pending_exit[symbol] = "시간청산"

            # 5) 평가금액 기록 (종가 기준)
            total = cash
            for p in positions.values():
                ps = self.series[p.symbol]
                pj = ps.index.get(date)
                if pj is not None:
                    total += p.qty * ps.closes[pj]
            equity.append((date, total))

        return {"trades": trades, "equity": equity, "end_capital": cash}
