"""
strategy_b.py — 전략 B: 20일선 눌림목 반등 (전략B설계.md 동결본)

출처: 이상우 《주식차트 절대비기 300선!》 전략 B + 손절·청산 규칙.
공통 골격(신호=종가 확정, 체결=익일 시가, 비용·사이징)은 strategy_a와 동일.

진입 (신호일 종가):
  - 20일선 > 60일선 (추세 전제)
  - 당일 저가 ≤ 20일선 ≤ 당일 종가 (터치 후 종가 회복)
  - 당일 거래량 ≤ 직전 20일 평균(당일 제외)의 0.5배 (소강)
  - 망치형(아래꼬리 ≥ 몸통 2배, 윗꼬리 ≤ 몸통) 또는 십자형(몸통 ≤ 고저폭 10%)
청산:
  - 절대 손절 -5% (장중, 체결가 기준)
  - 종가 < 20일선 → 익일 시가 (추세이탈)
  - 60일 고점 95% 이상에서 유성형 또는 거래량 3배 → 익일 시가 (천정청산)
  - 시간 청산 없음

주의: 이 전략의 홀드아웃 검증은 사전 약정 변경 하에 수행된다 (전략B설계.md §0).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .backtest import BTTrade
from .breakout import BreakoutBacktester
from .strategy_a import prior_mean, rolling_max
from .swing import DailyBar, sma

logger = logging.getLogger("autotrader.strategy_b")


@dataclass
class StrategyBConfig:
    ma_fast: int = 20
    ma_slow: int = 60
    pullback_volume_max: float = 0.5
    avg_volume_days: int = 20
    hammer_lower_wick_ratio: float = 2.0
    hammer_upper_wick_max: float = 1.0
    doji_body_max_ratio: float = 0.10
    stop_pct: float = 5.0
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
    def from_config(cls, config: dict) -> "StrategyBConfig":
        section = (config or {}).get("strategy_b", {}) or {}
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in section.items() if k in known})


def is_reversal_candle(bar: DailyBar, cfg: StrategyBConfig) -> bool:
    """망치형 또는 십자형인가."""
    body = abs(bar.close - bar.open)
    day_range = bar.high - bar.low
    if day_range <= 0:
        return False
    if body <= day_range * cfg.doji_body_max_ratio:
        return True                                    # 십자형 (몸통이 극히 작음)
    lower_wick = min(bar.open, bar.close) - bar.low
    upper_wick = bar.high - max(bar.open, bar.close)
    return (lower_wick >= body * cfg.hammer_lower_wick_ratio
            and upper_wick <= body * cfg.hammer_upper_wick_max)


@dataclass
class _PosB:
    symbol: str
    entry_date: str
    entry_price: float
    qty: int
    stop_price: float
    entry_cost: float
    entry_index: int


@dataclass
class _SeriesB:
    bars: list[DailyBar]
    index: dict[str, int]
    closes: list[float]
    ma_fast: list[Optional[float]]
    ma_slow: list[Optional[float]]
    avg_vol: list[Optional[float]]
    avg_value: list[Optional[float]]
    high_lookback: list[Optional[float]]


class StrategyBBacktester(BreakoutBacktester):
    """전략 B 백테스터. 비용·체결 헬퍼는 BreakoutBacktester 것을 상속한다."""

    def __init__(self, bars_by_symbol: dict[str, list[DailyBar]],
                 cfg: StrategyBConfig, base_config: dict):
        cost = (base_config or {}).get("cost", {}) or {}
        self.commission_pct = float(cost.get("commission_pct", 0.015))
        self.tax_pct = float(cost.get("tax_pct", 0.20))
        execution = (base_config or {}).get("execution", {}) or {}
        self.slippage_bps = int(execution.get("slippage_bps", 5))
        self.cfg = cfg  # type: ignore[assignment]
        self.b_cfg = cfg

        self.series_b: dict[str, _SeriesB] = {}
        for symbol, bars in bars_by_symbol.items():
            if not bars:
                continue
            closes = [b.close for b in bars]
            volumes = [float(b.volume) for b in bars]
            values = [b.value_krw for b in bars]
            self.series_b[symbol] = _SeriesB(
                bars=bars,
                index={b.date: i for i, b in enumerate(bars)},
                closes=closes,
                ma_fast=sma(closes, cfg.ma_fast),
                ma_slow=sma(closes, cfg.ma_slow),
                avg_vol=prior_mean(volumes, cfg.avg_volume_days),
                avg_value=sma(values, 20),
                high_lookback=rolling_max([b.high for b in bars], cfg.high_lookback_days),
            )
        self.all_dates = sorted({b.date for bars in bars_by_symbol.values() for b in bars})

    def _regime_ok(self, date: str) -> bool:
        s = self.series_b.get(self.b_cfg.regime_symbol)
        if s is None:
            return True
        i = s.index.get(date)
        if i is None or s.ma_fast[i] is None:
            return True
        return s.closes[i] > s.ma_fast[i]

    def _entry_signal(self, symbol: str, date: str) -> Optional[dict]:
        cfg = self.b_cfg
        s = self.series_b[symbol]
        i = s.index.get(date)
        if i is None:
            return None
        ma_f, ma_s = s.ma_fast[i], s.ma_slow[i]
        if ma_f is None or ma_s is None:
            return None
        if ma_f <= ma_s:
            return None                                    # 추세 전제 불만족
        bar = s.bars[i]
        if not (bar.low <= ma_f <= bar.close):
            return None                                    # 터치 후 회복 아님
        avg_vol = s.avg_vol[i]
        if avg_vol is None or avg_vol <= 0:
            return None
        if bar.volume > avg_vol * cfg.pullback_volume_max:
            return None                                    # 거래량 소강 아님
        if not is_reversal_candle(bar, cfg):
            return None                                    # 반전 캔들 아님
        if s.avg_value[i] is None or s.avg_value[i] < cfg.min_avg_value_krw:
            return None                                    # 유동성 부족
        return {"prev_close": s.closes[i], "avg_value": s.avg_value[i]}

    def _close_exit_reason(self, pos: _PosB, date: str) -> Optional[str]:
        cfg = self.b_cfg
        s = self.series_b[pos.symbol]
        j = s.index.get(date)
        if j is None:
            return None
        bar = s.bars[j]
        ma_f = s.ma_fast[j]
        if ma_f is not None and bar.close < ma_f:
            return "추세이탈"
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
        cfg = self.b_cfg
        dates = self.all_dates
        if window:
            dates = [d for d in dates if window[0] <= d <= window[1]]
        if not dates:
            return {"trades": [], "equity": [], "end_capital": capital}

        date_pos = {d: gi for gi, d in enumerate(self.all_dates)}
        cash = capital
        positions: dict[str, _PosB] = {}
        pending_exit: dict[str, str] = {}
        trades: list[BTTrade] = []
        equity: list[tuple[str, float]] = []

        def close_position(pos: _PosB, date: str, raw_price: float, reason: str) -> None:
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

            # 1) 예약 청산 (추세이탈/천정)을 시가에 실행
            for symbol, reason in list(pending_exit.items()):
                if symbol not in positions:
                    pending_exit.pop(symbol, None)
                    continue
                s = self.series_b[symbol]
                j = s.index.get(date)
                if j is not None:
                    close_position(positions[symbol], date, s.bars[j].open, reason)

            # 2) 절대 손절 -5% (장중)
            for symbol, pos in list(positions.items()):
                s = self.series_b[symbol]
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
                        ps = self.series_b[p.symbol]
                        pj = ps.index.get(prev_date)
                        equity_now += p.qty * (ps.closes[pj] if pj is not None else p.entry_price)
                    signals = []
                    for symbol in self.series_b:
                        if symbol == cfg.regime_symbol or symbol in positions:
                            continue
                        sig = self._entry_signal(symbol, prev_date)
                        if sig:
                            signals.append((sig["avg_value"], symbol, sig))
                    signals.sort(key=lambda x: -x[0])   # 거래대금 큰 순 (결정적 순서)
                    entered = 0
                    for _av, symbol, sig in signals:
                        if entered >= cfg.max_entries_per_day:
                            break
                        if len(positions) >= cfg.max_positions:
                            break
                        s = self.series_b[symbol]
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
                        positions[symbol] = _PosB(
                            symbol=symbol, entry_date=date, entry_price=fill, qty=qty,
                            stop_price=stop, entry_cost=buy_cost, entry_index=gi,
                        )
                        entered += 1

            # 4) 종가 판정: 추세이탈/천정 → 내일 시가 청산 예약. 마지막 날은 강제청산
            for symbol, pos in list(positions.items()):
                if is_last:
                    s = self.series_b[symbol]
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
                ps = self.series_b[p.symbol]
                pj = ps.index.get(date)
                if pj is not None:
                    total += p.qty * ps.closes[pj]
            equity.append((date, total))

        return {"trades": trades, "equity": equity, "end_capital": cash}
