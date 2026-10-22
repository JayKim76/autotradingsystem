"""validation 모듈 테스트 — 지표·폴드·게이트·워크포워드 스모크"""
from src.backtest import BTTrade
from src.validation import (
    compute_metrics, split_folds, trade_r, verdict, walk_forward,
)


def _trade(net, date="20260105", r_unit=2.0, reason="익절1차", entry=10_000.0, qty=10):
    gross = net + 300.0  # 비용 300 가정
    return BTTrade(symbol="005930", entry_time="093500", exit_time="103500",
                   entry_price=entry, exit_price=entry + net / qty, qty=qty,
                   reason=reason, gross=gross, cost=300.0,
                   date=date, r_unit_pct=r_unit)


def test_trade_r_conversion():
    # 리스크액 = 10,000 × 10주 × 2% = 2,000원 → +2,000원이면 +1R
    assert trade_r(_trade(2_000.0), fallback_r_pct=9.9) == 1.0
    assert trade_r(_trade(-1_000.0), fallback_r_pct=9.9) == -0.5


def test_compute_metrics_basics():
    trades = [_trade(2_000.0), _trade(-1_000.0), _trade(4_000.0, reason="손절(ATR)")]
    m = compute_metrics(trades, fallback_r_pct=2.0)
    assert m["trades"] == 3
    assert m["wins"] == 2
    assert m["win_rate"] > 66.0
    assert m["expectancy_r"] == (1.0 - 0.5 + 2.0) / 3
    assert m["profit_factor"] == 6_000.0 / 1_000.0
    assert set(m["exit_reasons"]) == {"익절1차", "손절"}
    assert m["monthly"]["202601"]["count"] == 3


def test_split_folds_contiguous():
    dates = [f"202601{d:02d}" for d in range(1, 11)]
    folds = split_folds(dates, 4)
    assert [len(f) for f in folds] == [2, 2, 2, 4]
    assert [d for f in folds for d in f] == dates  # 순서·누락 없음


def test_verdict_insufficient_when_few_trades():
    m = {"trades": 5, "expectancy_r": 1.0, "profit_factor": 3.0,
         "positive_fold_ratio": 1.0, "max_drawdown_pct": 1.0}
    assert verdict(m)["status"] == "INSUFFICIENT"


def test_verdict_pass_and_fail():
    good = {"trades": 50, "expectancy_r": 0.2, "profit_factor": 1.5,
            "positive_fold_ratio": 0.75, "max_drawdown_pct": 8.0}
    assert verdict(good)["status"] == "PASS"
    bad = {**good, "expectancy_r": -0.1}
    assert verdict(bad)["status"] == "FAIL"


def test_walk_forward_smoke_with_scenarios():
    """합성 시나리오 데이터로 파이프라인이 끝까지 도는지만 확인 (수익성 증거 아님)"""
    from src.scenarios import build
    scenario = build("trend_up")
    data = {}
    for i, date in enumerate(["20260105", "20260106", "20260107", "20260108"]):
        candles = []
        for c in scenario.candles:
            factor = 1.0 + i * 0.001
            candles.append(type(c)(
                time=c.time, open=c.open * factor, high=c.high * factor,
                low=c.low * factor, close=c.close * factor, volume=c.volume,
            ))
        data[date] = {"005930": candles}

    config = {
        "strategy": {"min_score": 3, "trading_start": "09:30", "trading_end": "15:00"},
        "risk": {"max_concurrent": 2, "max_daily_trades": 10},
        "stop_loss": {"mode": "atr", "sl_pct": 1.5},
        "take_profit": {"mode": "r_multiple", "tp1_r": 2.0, "tp2_r": 3.5},
        "cost": {"commission_pct": 0.015, "tax_pct": 0.2, "slippage_pct": 0.05},
    }
    result = walk_forward(data, config, capital=10_000_000, n_folds=2, optimize=False)
    assert result["n_folds"] == 2
    assert len(result["folds"]) == 2
    assert "expectancy_r" in result["oos_metrics"]
    v = verdict(result["oos_metrics"])
    assert v["status"] in ("PASS", "FAIL", "INSUFFICIENT")
