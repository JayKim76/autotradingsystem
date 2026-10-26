"""strategy_a: 240일선 돌파 & 거래량 급증 (전략A설계.md) 회귀 테스트"""
from datetime import date, timedelta

from src.strategy_a import StrategyABacktester, StrategyAConfig
from src.swing import DailyBar

BASE_CFG = {"cost": {"commission_pct": 0.015, "tax_pct": 0.20},
            "execution": {"slippage_bps": 5}}


def _bars(n, price_fn, vol=100_000):
    """n일치 일봉. price_fn(i) -> (open, high, low, close)"""
    out = []
    d = date(2020, 1, 1)
    for i in range(n):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        o, h, l, c = price_fn(i)
        v = vol(i) if callable(vol) else vol
        out.append(DailyBar(date=d.strftime("%Y%m%d"), open=o, high=h, low=l,
                            close=c, volume=v))
        d += timedelta(days=1)
    return out


def _flat_then_breakout(breakout_vol_mult=6.0, wick_ratio=0.2, already_above=False):
    """250일 횡보(종가 100) 후 마지막 날 240일선 돌파 양봉.

    횡보 구간 거래량 100,000주·가격 100원 → 거래대금 1,000만원이라
    유동성 필터(50억)를 못 넘으므로 min_avg_value_krw를 낮춘 설정으로 테스트한다.
    """
    def price(i):
        if i < 249:
            return (100, 100.5, 99.5, 100)
        # 돌파일: 시가 100, 종가 104 (몸통 4), 윗꼬리 = 몸통 × wick_ratio
        return (100, 104 + 4 * wick_ratio, 99.8, 104)

    def vol(i):
        return int(100_000 * breakout_vol_mult) if i == 249 else 100_000

    bars = _bars(250, price, vol)
    if already_above:
        # 전일도 이미 240일선 위 → '돌파'가 아님
        bars[248] = DailyBar(date=bars[248].date, open=100, high=105, low=99.5,
                             close=104.5, volume=100_000)
    return bars


def _cfg():
    return StrategyAConfig(min_avg_value_krw=0)


def test_entry_signal_fires_on_textbook_breakout():
    bt = StrategyABacktester({"AAA": _flat_then_breakout()}, _cfg(), BASE_CFG)
    sig = bt._entry_signal("AAA", bt.series_a["AAA"].bars[-1].date)
    assert sig is not None
    assert sig["structure_stop"] == 102.0     # (100 + 104) / 2
    assert sig["volume_ratio"] >= 5.0


def test_entry_signal_blocked_without_volume_surge():
    bars = _flat_then_breakout(breakout_vol_mult=2.0)
    bt = StrategyABacktester({"AAA": bars}, _cfg(), BASE_CFG)
    assert bt._entry_signal("AAA", bars[-1].date) is None


def test_entry_signal_blocked_by_long_upper_wick():
    bars = _flat_then_breakout(wick_ratio=0.9)
    bt = StrategyABacktester({"AAA": bars}, _cfg(), BASE_CFG)
    assert bt._entry_signal("AAA", bars[-1].date) is None


def test_entry_signal_requires_fresh_cross():
    bars = _flat_then_breakout(already_above=True)
    bt = StrategyABacktester({"AAA": bars}, _cfg(), BASE_CFG)
    assert bt._entry_signal("AAA", bars[-1].date) is None


def test_run_structure_exit_after_failed_breakout():
    """돌파 다음날 진입 → 종가가 몸통 중심(102) 아래로 마감하면 돌파실패 청산."""
    bars = _flat_then_breakout()

    def tail(i):
        if i == 250:   # 진입일 (시가 104, 종가 유지)
            return (104, 104.5, 103.5, 104)
        if i == 251:   # 종가 101 < 중심 102 → 돌파실패 확정
            return (103, 103.5, 100.5, 101)
        return (101, 101.5, 100.5, 101)  # 청산일 (시가 체결)

    extra = _bars(3, lambda i: tail(250 + i))
    # 날짜가 이어지도록 기존 마지막 날짜 이후로 조정
    from datetime import datetime
    last = datetime.strptime(bars[-1].date, "%Y%m%d").date()
    fixed = []
    d = last
    for b in extra:
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        fixed.append(DailyBar(date=d.strftime("%Y%m%d"), open=b.open, high=b.high,
                              low=b.low, close=b.close, volume=100_000))
    bt = StrategyABacktester({"AAA": bars + fixed}, _cfg(), BASE_CFG)
    result = bt.run(capital=10_000_000)
    reasons = [t.reason for t in result["trades"]]
    assert reasons == ["돌파실패"], reasons


def test_run_absolute_stop():
    """진입 후 -7% 아래로 밀리면 손절 청산."""
    bars = _flat_then_breakout()
    extra_specs = [(104, 104.5, 103.5, 104),      # 진입일
                   (100, 100.5, 96.0, 97)]        # 저가 96 < 손절가(104×0.93≈96.7)
    from datetime import datetime
    last = datetime.strptime(bars[-1].date, "%Y%m%d").date()
    fixed = []
    d = last
    for o, h, l, c in extra_specs:
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        fixed.append(DailyBar(date=d.strftime("%Y%m%d"), open=o, high=h, low=l,
                              close=c, volume=100_000))
    bt = StrategyABacktester({"AAA": bars + fixed}, _cfg(), BASE_CFG)
    result = bt.run(capital=10_000_000)
    reasons = [t.reason for t in result["trades"]]
    assert reasons == ["손절"], reasons
