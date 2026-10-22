"""
final_rules.py — 원칙 기반 통합 규칙 (최종규칙설계.md 동결본)

구현은 레짐 조건부 돌파(RegimeBreakoutBacktester)를 그대로 쓰고,
파라미터만 고전 돈치안 시스템 2 표준값(55일 돌파/20일 청산)으로 바꾼 것이다.
검증 대상은 설계에 쓰지 않은 홀드아웃 구간(2013~2023-10-23)뿐이다.
"""
from __future__ import annotations

from dataclasses import dataclass

from .regime_breakout import RegimeBreakoutConfig
from .swing import DailyBar


@dataclass
class FinalRulesConfig(RegimeBreakoutConfig):
    breakout_days: int = 55        # 돈치안 시스템 2 표준 (외부 문헌값)
    exit_channel_days: int = 20    # 돈치안 시스템 2 표준 (외부 문헌값)

    @classmethod
    def from_config(cls, config: dict) -> "FinalRulesConfig":
        section = (config or {}).get("final_rules", {}) or {}
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in section.items() if k in known})


def filter_bars_until(bars_by_symbol: dict[str, list[DailyBar]],
                      end_date: str) -> dict[str, list[DailyBar]]:
    """홀드아웃 전용: end_date(YYYYMMDD) 이후 바를 잘라낸다.

    설계에 사용된 구간(2023-10-24~)이 검증에 섞이지 않게 하는 절단 함수.
    """
    out: dict[str, list[DailyBar]] = {}
    for symbol, bars in bars_by_symbol.items():
        kept = [b for b in bars if b.date <= end_date]
        if kept:
            out[symbol] = kept
    return out
