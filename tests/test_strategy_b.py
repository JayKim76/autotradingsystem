"""strategy_b: 20일선 눌림목 반등 (전략B설계.md) 회귀 테스트"""
from datetime import date, datetime, timedelta

from src.strategy_b import StrategyBBacktester, StrategyBConfig
from src.swing import DailyBar

BASE_CFG = {"cost": {"commission_pct": 0.015, "tax_pct": 0.20},
            "execution": {"slippage_bps": 5}}


def _series(n_base, signal_bar, tail_specs=()):
    """상승 추세(종가 100 + 0.5*i) n_base일 + 신호봉 + 꼬리 봉들."""
    bars = []
    d = date(2020, 1, 1)
    for i in range(n_base):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        c = 100 + 0.5 * i
        bars.append(DailyBar(date=d.strftime("%Y%m%d"), open=c - 0.2, high=c + 0.3,
                             low=c - 0.3, close=c, volume=100_000))
        d += timedelta(days=1)
    for spec in (signal_bar,) + tuple(tail_specs):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        o, h, l, c, v = spec
        bars.append(DailyBar(date=d.strftime("%Y%m%d"), open=o, high=h, low=l,
                             close=c, volume=v))
        d += timedelta(days=1)
    return bars


# 기준 추세 100일: 마지막 종가 149.5, 신호일 20일선은 약 145 내외
HAMMER = (148.0, 147.5, 143.0, 147.0, 40_000)       # 몸통 1, 아래꼬리 4, 거래량 0.4배


def _cfg():
    return StrategyBConfig(min_avg_value_krw=0)


def _signal(bars):
    bt = StrategyBBacktester({"AAA": bars}, _cfg(), BASE_CFG)
    return bt._entry_signal("AAA", bars[100].date)


def test_entry_signal_fires_on_hammer_touch():
    bars = _series(100, HAMMER)
    assert _signal(bars) is not None


def test_entry_signal_blocked_without_reversal_candle():
    plain = (150.0, 150.2, 145.0, 147.0, 40_000)    # 몸통 3, 아래꼬리 2 < 2배
    assert _signal(_series(100, plain)) is None


def test_entry_signal_blocked_when_volume_not_dry():
    loud = (148.0, 147.5, 143.0, 147.0, 80_000)     # 0.8배 — 소강 아님
    assert _signal(_series(100, loud)) is None


def test_entry_signal_blocked_when_close_below_ma20():
    under = (146.0, 146.5, 141.0, 142.0, 40_000)    # 종가가 20일선 아래 마감
    assert _signal(_series(100, under)) is None


def test_entry_signal_blocked_in_downtrend():
    bars = []
    d = date(2020, 1, 1)
    for i in range(100):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        c = 200 - 0.5 * i
        bars.append(DailyBar(date=d.strftime("%Y%m%d"), open=c + 0.2, high=c + 0.3,
                             low=c - 0.3, close=c, volume=100_000))
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    last_close = bars[-1].close
    bars.append(DailyBar(date=d.strftime("%Y%m%d"), open=last_close + 1, high=last_close + 1.5,
                         low=last_close - 4, close=last_close + 0.5, volume=40_000))
    bt = StrategyBBacktester({"AAA": bars}, _cfg(), BASE_CFG)
    assert bt._entry_signal("AAA", bars[-1].date) is None


def test_run_trend_break_exit():
    """신호 → 진입 → 종가가 20일선 아래로 마감하면 추세이탈 청산."""
    tail = [
        (147.0, 147.6, 146.5, 147.5, 100_000),   # 진입일
        (147.0, 147.2, 143.0, 143.5, 100_000),   # 종가 < 20일선 → 이탈 확정
        (143.0, 143.4, 142.5, 143.0, 100_000),   # 청산일 (시가 체결)
    ]
    bars = _series(100, HAMMER, tail)
    bt = StrategyBBacktester({"AAA": bars}, _cfg(), BASE_CFG)
    result = bt.run(capital=10_000_000)
    reasons = [t.reason for t in result["trades"]]
    assert reasons == ["추세이탈"], reasons


def test_run_absolute_stop():
    """진입 후 -5% 아래로 밀리면 손절 청산."""
    tail = [
        (147.0, 147.6, 146.5, 147.5, 100_000),   # 진입일
        (146.0, 146.2, 138.0, 139.0, 100_000),   # 저가 < 손절가(147×0.95≈139.7)
    ]
    bars = _series(100, HAMMER, tail)
    bt = StrategyBBacktester({"AAA": bars}, _cfg(), BASE_CFG)
    result = bt.run(capital=10_000_000)
    reasons = [t.reason for t in result["trades"]]
    assert reasons == ["손절"], reasons
