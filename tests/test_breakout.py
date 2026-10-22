"""breakout: 돌파형 스윙 전략 회귀 테스트"""
from datetime import datetime, timedelta

from src.breakout import (BreakoutBacktester, BreakoutConfig, donchian_high,
                          donchian_low)
from src.swing import DailyBar, swing_walk_forward
from src.validation import verdict

BASE_CONFIG = {"cost": {"commission_pct": 0.015, "tax_pct": 0.20},
               "execution": {"slippage_bps": 5}}


def _bars(closes, volumes):
    base = datetime(2025, 1, 1)
    bars = []
    prev = closes[0]
    for i, (c, v) in enumerate(zip(closes, volumes)):
        o = prev
        bars.append(DailyBar(date=(base + timedelta(days=i)).strftime("%Y%m%d"),
                             open=o, high=max(o, c) * 1.002, low=min(o, c) * 0.998,
                             close=c, volume=v))
        prev = c
    return bars


def _breakout_closes():
    closes = []
    base = [10000.0, 10050.0, 10000.0, 9950.0]
    for i in range(30):
        closes.append(base[i % 4])
    price = 10600.0                      # 돌파일 (인덱스 30)
    closes.append(price)
    for _ in range(10):                  # 상승 지속
        price *= 1.01
        closes.append(price)
    closes.append(11000.0)               # 급락 — 채널 이탈 (인덱스 41)
    for _ in range(4):
        closes.append(11000.0)
    return closes


def _test_config(**overrides):
    params = dict(breakout_days=10, exit_channel_days=5, volume_mult=1.5,
                  atr_period=7, stop_atr_mult=2.0, max_hold_days=50,
                  min_avg_value_krw=1.0, regime_symbol="NONE")
    params.update(overrides)
    return BreakoutConfig(**params)


def test_donchian_shifted_no_lookahead():
    bars = _bars([100.0] * 10 + [120.0], [100] * 11)
    highs = donchian_high(bars, 10)
    assert highs[9] is None
    # 돌파일(인덱스 10) 판정에는 오늘 고가가 포함되지 않는다
    assert abs(highs[10] - 100.0 * 1.002) < 0.01
    lows = donchian_low(bars, 10)
    assert abs(lows[10] - 100.0 * 0.998) < 0.01


def test_breakout_entry_and_channel_exit():
    closes = _breakout_closes()
    volumes = [300_000] * len(closes)
    volumes[30] = 1_200_000              # 돌파일 거래량 급증
    bars = _bars(closes, volumes)
    bt = BreakoutBacktester({"AAA": bars}, _test_config(), BASE_CONFIG)

    assert bt._entry_signal("AAA", bars[30].date) is not None
    result = bt.run(capital=10_000_000)
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade.entry_time == bars[31].date          # 신호 다음날 시가 체결
    assert trade.reason == "채널이탈"
    assert trade.exit_time == bars[42].date           # 이탈 판정 다음날 시가
    assert trade.net > 0                              # 승자를 채널까지 보유


def test_no_volume_no_entry():
    closes = _breakout_closes()
    volumes = [300_000] * len(closes)    # 거래량 급증 없음
    bars = _bars(closes, volumes)
    bt = BreakoutBacktester({"AAA": bars}, _test_config(), BASE_CONFIG)
    assert bt._entry_signal("AAA", bars[30].date) is None
    assert bt.run(capital=10_000_000)["trades"] == []


def test_breakout_walk_forward_smoke():
    closes = _breakout_closes() * 4
    volumes = [300_000] * len(closes)
    for i in range(30, len(closes), 46):
        volumes[i] = 1_200_000
    bars_by_symbol = {sym: _bars(closes, volumes) for sym in ("AAA", "BBB")}
    result = swing_walk_forward(bars_by_symbol, _test_config(), BASE_CONFIG,
                                capital=10_000_000, n_folds=2, warmup_days=20,
                                backtester_cls=BreakoutBacktester)
    assert len(result["folds"]) == 2
    v = verdict(result["oos_metrics"])
    assert v["status"] in ("PASS", "FAIL", "INSUFFICIENT")
