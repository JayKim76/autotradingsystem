"""regime_breakout: 브레드스 게이트 회귀 테스트"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_breakout import BASE_CONFIG, _bars  # noqa: E402

from src.breakout import BreakoutBacktester, BreakoutConfig  # noqa: E402
from src.regime_breakout import (RegimeBreakoutBacktester,  # noqa: E402
                                 RegimeBreakoutConfig, compute_breadth)
from src.swing import swing_walk_forward  # noqa: E402
from src.validation import verdict  # noqa: E402


def _series(closes, volume=300_000):
    return _bars(closes, [volume] * len(closes))


def _rising(days=90, start=10_000.0, rate=1.005):
    out, p = [], start
    for _ in range(days):
        p *= rate
        out.append(p)
    return out


def _falling(days=90, start=20_000.0, rate=0.995):
    out, p = [], start
    for _ in range(days):
        p *= rate
        out.append(p)
    return out


def test_compute_breadth_ratio():
    bars = {
        "AAA": _series(_rising()),
        "BBB": _series(_falling()),
        "CCC": _series(_rising(start=5_000.0)),
    }
    breadth = compute_breadth(bars, ma_period=60, min_symbols=3)
    last_date = bars["AAA"][-1].date
    assert abs(breadth[last_date] - 2 / 3) < 1e-9
    # 유효 종목이 부족하면 그 날짜는 판정에서 제외된다
    assert compute_breadth(bars, ma_period=60, min_symbols=4) == {}


def _aaa_breakout_long():
    closes = []
    base = [10000.0, 10050.0, 10000.0, 9950.0]
    for i in range(70):
        closes.append(base[i % 4])
    price = 10600.0
    closes.append(price)                 # 돌파일 (인덱스 70)
    for _ in range(10):
        price *= 1.01
        closes.append(price)
    closes.append(11000.0)
    for _ in range(4):
        closes.append(11000.0)
    return closes


def _rb_config(**overrides):
    params = dict(breakout_days=10, exit_channel_days=5, volume_mult=1.5,
                  atr_period=7, stop_atr_mult=2.0, max_hold_days=50,
                  min_avg_value_krw=1.0, regime_symbol="NONE",
                  breadth_ma_period=60, breadth_threshold=0.5,
                  breadth_min_symbols=3)
    params.update(overrides)
    return RegimeBreakoutConfig(**params)


def test_breadth_gate_blocks_entries_in_weak_market():
    aaa_closes = _aaa_breakout_long()
    volumes = [300_000] * len(aaa_closes)
    volumes[70] = 1_200_000
    aaa = _bars(aaa_closes, volumes)
    weak = {
        "AAA": aaa,
        "BBB": _series(_falling(days=len(aaa_closes))),
        "CCC": _series(_falling(days=len(aaa_closes), start=30_000.0)),
    }

    plain = BreakoutBacktester(weak, BreakoutConfig(
        breakout_days=10, exit_channel_days=5, volume_mult=1.5, atr_period=7,
        stop_atr_mult=2.0, max_hold_days=50, min_avg_value_krw=1.0,
        regime_symbol="NONE"), BASE_CONFIG)
    assert len(plain.run(capital=10_000_000)["trades"]) >= 1   # 게이트 없으면 진입

    gated = RegimeBreakoutBacktester(weak, _rb_config(), BASE_CONFIG)
    signal_date = aaa[70].date
    assert gated.breadth_at(signal_date) is not None
    assert gated.breadth_at(signal_date) < 0.5                  # 1/3만 60일선 위
    assert gated.run(capital=10_000_000)["trades"] == []        # 게이트가 막는다


def test_regime_walk_forward_smoke():
    bars = {sym: _series(_rising(days=200)) for sym in ("AAA", "BBB", "CCC")}
    result = swing_walk_forward(bars, _rb_config(breadth_min_symbols=3),
                                BASE_CONFIG, capital=10_000_000, n_folds=2,
                                warmup_days=60,
                                backtester_cls=RegimeBreakoutBacktester)
    assert len(result["folds"]) == 2
    assert verdict(result["oos_metrics"])["status"] in ("PASS", "FAIL", "INSUFFICIENT")
