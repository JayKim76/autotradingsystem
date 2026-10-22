"""swing: 일봉 스윙 전략 회귀 테스트"""
from datetime import datetime, timedelta

from src.indicators import Candle
from src.swing import (DailyBar, SwingBacktester, SwingConfig, aggregate_daily,
                       atr_series, rsi_series, sma, swing_walk_forward)
from src.validation import verdict

BASE_CONFIG = {"cost": {"commission_pct": 0.015, "tax_pct": 0.20},
               "execution": {"slippage_bps": 5}}


def _dates(n, start="20250101"):
    base = datetime.strptime(start, "%Y%m%d")
    return [(base + timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]


def _bars_from_closes(closes, volume=300_000):
    dates = _dates(len(closes))
    bars = []
    prev = closes[0]
    for d, c in zip(dates, closes):
        o = prev
        bars.append(DailyBar(date=d, open=o, high=max(o, c) * 1.002,
                             low=min(o, c) * 0.998, close=c, volume=volume))
        prev = c
    return bars


def _pattern_closes():
    """상승 30일 → 눌림 3일 → 반등 1일 → 완만 상승 12일"""
    closes = []
    price = 10_000.0
    for _ in range(30):
        price *= 1.008
        closes.append(price)
    for _ in range(3):
        price *= 0.985
        closes.append(price)
    price *= 1.012
    closes.append(price)          # 반등일 (인덱스 33) — 신호 발생
    for _ in range(12):
        price *= 1.003
        closes.append(price)
    return closes


def _test_config(**overrides):
    params = dict(ma_fast=5, ma_slow=20, atr_period=7, rsi_period=7,
                  rsi_pullback=55.0, pullback_lookback=3, stop_atr_mult=2.0,
                  max_hold_days=5, min_avg_value_krw=1.0, regime_symbol="NONE")
    params.update(overrides)
    return SwingConfig(**params)


def test_aggregate_daily_ohlcv():
    candles = [
        Candle(time="090000", open=100, high=105, low=99, close=102, volume=10),
        Candle(time="100000", open=102, high=110, low=101, close=108, volume=20),
    ]
    out = aggregate_daily({"20250102": {"005930": candles}})
    bar = out["005930"][0]
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (100, 110, 99, 108, 30)


def test_indicators_basic():
    values = [float(i) for i in range(1, 11)]
    s = sma(values, 5)
    assert s[3] is None and s[4] == 3.0 and s[9] == 8.0
    rising = [100.0 + i for i in range(30)]
    r = rsi_series(rising, 14)
    assert r[-1] == 100.0
    bars = _bars_from_closes(rising)
    a = atr_series(bars, 14)
    assert a[-1] is not None and a[-1] > 0


def test_entry_signal_and_time_exit():
    bars = _bars_from_closes(_pattern_closes())
    bt = SwingBacktester({"AAA": bars}, _test_config(), BASE_CONFIG)

    signal = bt._entry_signal("AAA", bars[33].date)
    assert signal is not None                      # 반등일에 신호 발생
    assert bt._entry_signal("AAA", bars[10].date) is None   # warmup 구간은 신호 없음

    result = bt.run(capital=10_000_000)
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    # 체결은 신호 다음날 시가 (슬리피지 반영)
    assert trade.entry_time == bars[34].date
    assert abs(trade.entry_price - bars[34].open * 1.0005) < 0.01
    # 완만 상승이라 손절/추세이탈 없이 시간청산 (보유 5일 → 다음날 시가)
    assert trade.reason == "시간청산"
    assert trade.exit_time == bars[40].date
    assert trade.r_unit_pct > 0
    assert abs(trade.net - (trade.gross - trade.cost)) < 0.01


def test_gap_down_stop_exits_at_open():
    closes = _pattern_closes()
    bars = _bars_from_closes(closes)
    # 진입 다음날(인덱스 35) 시가가 -15% 갭하락 → 손절가 아래라 시가 체결
    crash_open = bars[35].open * 0.85
    bars[35] = DailyBar(date=bars[35].date, open=crash_open,
                        high=crash_open * 1.01, low=crash_open * 0.99,
                        close=crash_open, volume=300_000)
    bt = SwingBacktester({"AAA": bars}, _test_config(), BASE_CONFIG)
    result = bt.run(capital=10_000_000)
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade.reason == "손절"
    assert trade.exit_time == bars[35].date
    assert abs(trade.exit_price - crash_open * 0.9995) < 0.01
    assert trade.net < 0


def test_no_entry_without_regime_symbol_data_is_allowed():
    # 레짐 심볼 데이터가 없으면 필터 비활성 (설계 문서 명시 동작)
    bars = _bars_from_closes(_pattern_closes())
    cfg = _test_config(regime_symbol="069500")
    bt = SwingBacktester({"AAA": bars}, cfg, BASE_CONFIG)
    assert bt._regime_ok(bars[33].date) is True


def test_walk_forward_smoke():
    closes = _pattern_closes() * 5     # 패턴 반복으로 표본 확보
    bars_by_symbol = {sym: _bars_from_closes(closes) for sym in ("AAA", "BBB", "CCC")}
    result = swing_walk_forward(bars_by_symbol, _test_config(), BASE_CONFIG,
                                capital=10_000_000, n_folds=2, warmup_days=20)
    assert len(result["folds"]) == 2
    assert result["oos_metrics"]["trades"] >= 1
    v = verdict(result["oos_metrics"])
    assert v["status"] in ("PASS", "FAIL", "INSUFFICIENT")
