"""
regime_breakout.py — 레짐 조건부 돌파 (돌파형 + 브레드스 게이트)

돌파형(BreakoutBacktester)과 진입·청산·자금 관리는 완전히 동일하고,
신규 진입 조건에 시장 브레드스 게이트 하나만 추가한 새 가설이다.
설계와 데이터 재사용 고지: 레짐돌파전략설계.md 참조.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .breakout import BreakoutBacktester, BreakoutConfig
from .swing import DailyBar, sma


def compute_breadth(bars_by_symbol: dict[str, list[DailyBar]],
                    ma_period: int = 60, min_symbols: int = 20,
                    exclude: Optional[str] = None) -> dict[str, float]:
    """날짜별 브레드스: 종가가 자기 ma_period일선 위에 있는 종목 비율.

    유효 종목(그날 종가와 이평이 모두 있는 종목)이 min_symbols 미만인 날짜는
    결과에서 제외한다. 미래참조 없음 — 각 날짜의 값은 그날까지의 데이터만 쓴다.
    """
    per_symbol: dict[str, dict[str, tuple[float, Optional[float]]]] = {}
    for symbol, bars in bars_by_symbol.items():
        if symbol == exclude or not bars:
            continue
        closes = [b.close for b in bars]
        ma = sma(closes, ma_period)
        per_symbol[symbol] = {
            b.date: (b.close, ma[i]) for i, b in enumerate(bars)
        }

    dates = sorted({d for m in per_symbol.values() for d in m})
    out: dict[str, float] = {}
    for date in dates:
        above = valid = 0
        for m in per_symbol.values():
            point = m.get(date)
            if point is None:
                continue
            close, ma_value = point
            if ma_value is None:
                continue
            valid += 1
            if close > ma_value:
                above += 1
        if valid >= min_symbols:
            out[date] = above / valid
    return out


@dataclass
class RegimeBreakoutConfig(BreakoutConfig):
    breadth_ma_period: int = 60
    breadth_threshold: float = 0.5
    breadth_min_symbols: int = 20

    @classmethod
    def from_config(cls, config: dict) -> "RegimeBreakoutConfig":
        section = (config or {}).get("regime_breakout", {}) or {}
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in section.items() if k in known})


class RegimeBreakoutBacktester(BreakoutBacktester):
    """돌파형 + 브레드스 게이트. 게이트는 신규 진입에만 적용한다."""

    def __init__(self, bars_by_symbol: dict[str, list[DailyBar]],
                 cfg: RegimeBreakoutConfig, base_config: dict):
        super().__init__(bars_by_symbol, cfg, base_config)
        self.rb_cfg = cfg
        raw = compute_breadth(
            bars_by_symbol,
            ma_period=cfg.breadth_ma_period,
            min_symbols=cfg.breadth_min_symbols,
            exclude=cfg.regime_symbol,
        )
        # 유효 종목 부족으로 비는 날짜는 직전 값으로 이어 쓴다 (게이트 판정 연속성)
        self.breadth: dict[str, float] = {}
        last: Optional[float] = None
        for date in self.all_dates:
            if date in raw:
                last = raw[date]
            if last is not None:
                self.breadth[date] = last

    def breadth_at(self, date: str) -> Optional[float]:
        return self.breadth.get(date)

    def _regime_ok(self, date: str) -> bool:
        if not super()._regime_ok(date):            # 기존 지수 필터 (069500 > 20일선)
            return False
        value = self.breadth.get(date)
        if value is None:                            # 판정 불가 구간 — 보수적으로 닫는다
            return False
        return value >= self.rb_cfg.breadth_threshold
