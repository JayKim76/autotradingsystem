"""final_rules: 동결 규칙·홀드아웃 절단 회귀 테스트"""
from src.final_rules import FinalRulesConfig, filter_bars_until
from src.regime_breakout import RegimeBreakoutBacktester
from src.swing import DailyBar, swing_walk_forward
from src.validation import verdict

BASE_CONFIG = {"cost": {"commission_pct": 0.015, "tax_pct": 0.20},
               "execution": {"slippage_bps": 5}}


def test_final_rules_frozen_defaults():
    cfg = FinalRulesConfig()
    # 동결값: 돈치안 시스템 2 표준 (55-in / 20-out) + 브레드스 게이트
    assert cfg.breakout_days == 55
    assert cfg.exit_channel_days == 20
    assert cfg.breadth_threshold == 0.5
    loaded = FinalRulesConfig.from_config({"final_rules": {"breakout_days": 55}})
    assert loaded.breakout_days == 55 and loaded.exit_channel_days == 20


def test_filter_bars_until_cuts_used_period():
    bars = [DailyBar(date=d, open=1, high=1, low=1, close=1, volume=1)
            for d in ("20231020", "20231023", "20231024", "20240101")]
    out = filter_bars_until({"AAA": bars, "BBB": bars[:1]}, "20231023")
    assert [b.date for b in out["AAA"]] == ["20231020", "20231023"]
    assert [b.date for b in out["BBB"]] == ["20231020"]


def test_final_rules_walk_forward_smoke():
    from datetime import datetime, timedelta

    def rising(days=300, start=10_000.0):
        bars, p = [], start
        base = datetime(2020, 1, 2)
        prev = p
        for i in range(days):
            p *= 1.004
            bars.append(DailyBar(date=(base + timedelta(days=i)).strftime("%Y%m%d"),
                                 open=prev, high=p * 1.002, low=prev * 0.998,
                                 close=p, volume=1_000_000))
            prev = p
        return bars

    bars_by_symbol = {s: rising() for s in ("AAA", "BBB", "CCC")}
    cfg = FinalRulesConfig(breadth_min_symbols=3, min_avg_value_krw=1.0)
    result = swing_walk_forward(bars_by_symbol, cfg, BASE_CONFIG,
                                capital=10_000_000, n_folds=2, warmup_days=60,
                                backtester_cls=RegimeBreakoutBacktester)
    assert len(result["folds"]) == 2
    assert verdict(result["oos_metrics"])["status"] in ("PASS", "FAIL", "INSUFFICIENT")
