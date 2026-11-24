"""선물 TSMOM 테스트 — 방향 판정·변동성 타게팅·숏 수익·스티칭 연속성"""
import importlib.util
from datetime import date, timedelta
from pathlib import Path

from src.futures_tsmom import (
    FuturesTsmomBacktester, FuturesTsmomConfig, realized_vol_annual,
    tsmom_direction, vol_target_weight,
)
from src.swing import DailyBar, swing_walk_forward
from src.validation import verdict

_spec = importlib.util.spec_from_file_location(
    "collect_futures_data",
    Path(__file__).parent.parent / "scripts" / "collect_futures_data.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
stitch_continuous = _mod.stitch_continuous


def test_direction_sign():
    up = [100.0 + i for i in range(300)]
    down = [400.0 - i for i in range(300)]
    assert tsmom_direction(up, 299, 252) == 1
    assert tsmom_direction(down, 299, 252) == -1
    assert tsmom_direction(up, 100, 252) == 0  # 히스토리 부족


def test_vol_target_weight():
    assert vol_target_weight(0.20, target_vol=0.10) == 0.5
    assert vol_target_weight(0.02, target_vol=0.10, max_leverage=2.0) == 2.0  # 상한
    assert vol_target_weight(0.0) == 0.0


def test_realized_vol_annual_flat_is_zero():
    assert realized_vol_annual([100.0] * 100, 99) == 0.0


def _date_seq(n: int, start: date = date(2020, 1, 1)) -> list[str]:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return days


def _series_bars(prices: list[float]) -> dict[str, list[DailyBar]]:
    dates = _date_seq(len(prices))
    bars = [DailyBar(date=d, open=p, high=p, low=p, close=p, volume=1000)
            for d, p in zip(dates, prices)]
    return {"KOSPI200F": bars}


def _cfg() -> FuturesTsmomConfig:
    return FuturesTsmomConfig(symbol="KOSPI200F", lookback_days=60,
                              target_vol_annual=0.10, vol_window_days=20,
                              max_leverage=2.0, cost_pct_oneway=0.01)


def test_short_position_profits_in_downtrend():
    prices = [300.0 * (0.998 ** i) for i in range(200)]
    bt = FuturesTsmomBacktester(_series_bars(prices), _cfg(), {})
    result = bt.run(capital=10_000_000)
    assert result["trades"]
    assert result["end_capital"] > 10_000_000  # 하락장에서 숏으로 수익


def test_long_position_profits_in_uptrend():
    prices = [100.0 * (1.002 ** i) for i in range(200)]
    bt = FuturesTsmomBacktester(_series_bars(prices), _cfg(), {})
    result = bt.run(capital=10_000_000)
    assert result["end_capital"] > 10_000_000


def test_no_trades_before_lookback():
    prices = [100.0 * (1.002 ** i) for i in range(200)]
    dates = _date_seq(200)
    bt = FuturesTsmomBacktester(_series_bars(prices), _cfg(), {})
    result = bt.run(capital=10_000_000)
    first_entry = min(t.entry_time for t in result["trades"])
    assert first_entry > dates[60]


def test_walk_forward_smoke():
    prices = [100.0 * (1.001 ** i) * (1 + 0.02 * ((i % 40) - 20) / 20) for i in range(500)]
    result = swing_walk_forward(_series_bars(prices), _cfg(), {},
                                capital=10_000_000, n_folds=2, warmup_days=60,
                                backtester_cls=FuturesTsmomBacktester)
    assert len(result["folds"]) == 2
    assert result["oos_metrics"]["trades"] > 0
    assert verdict(result["oos_metrics"])["status"] in ("PASS", "FAIL", "INSUFFICIENT")


def test_parse_krx_portal_row():
    parse_bar = _mod._parse_bar
    row = {"일자": "2012/12/28", "시가": "260.10", "고가": "261.00",
           "저가": "259.50", "종가": "260.90", "거래량": "1,234"}
    bar = parse_bar(row)
    assert bar == {"date": "20121228", "open": 260.10, "high": 261.00,
                   "low": 259.50, "close": 260.90, "volume": 1234}
    assert _mod._expiry_from_filename("코스피200선물_201212") == "201212"
    assert _mod._expiry_from_filename("201212") == "201212"


def test_stitch_removes_roll_gap():
    """롤오버일의 가격 점프(100→102)가 수익률에 새지 않아야 한다"""
    def bar(d, c):
        return {"date": d, "open": c, "high": c, "low": c, "close": c, "volume": 1}
    old = [bar("20260302", 100.0), bar("20260303", 100.0), bar("20260304", 100.0)]
    new = [bar("20260303", 102.0), bar("20260304", 102.0), bar("20260305", 102.0)]
    out = stitch_continuous({"202603": old, "202606": new})
    closes = [r["close"] for r in out]
    # 조정 후 전 구간이 평탄해야 한다 (점프 제거)
    assert all(abs(c - closes[0]) < 1e-9 for c in closes)
