"""Deflated Sharpe 테스트 — 다중 검정 보정의 방향성과 경계 조건"""
from src.backtest import BTTrade
from src.validation import compute_metrics, deflated_sharpe_ratio


def _rs_from_pattern():
    # 평균이 양수인 R 분포 (승자 +1.5R × 20, 패자 -1.0R × 10)
    return [1.5] * 20 + [-1.0] * 10


def test_dsr_in_unit_interval():
    dsr = deflated_sharpe_ratio(_rs_from_pattern(), n_trials=7)
    assert 0.0 <= dsr <= 1.0


def test_dsr_decreases_with_more_trials():
    rs = _rs_from_pattern()
    dsr_1 = deflated_sharpe_ratio(rs, n_trials=1)
    dsr_7 = deflated_sharpe_ratio(rs, n_trials=7)
    dsr_100 = deflated_sharpe_ratio(rs, n_trials=100)
    assert dsr_1 > dsr_7 > dsr_100


def test_dsr_negative_strategy_is_low():
    rs = [-1.5] * 20 + [1.0] * 10
    assert deflated_sharpe_ratio(rs, n_trials=7) < 0.1


def test_dsr_edge_cases():
    assert deflated_sharpe_ratio([], n_trials=7) == 0.0
    assert deflated_sharpe_ratio([1.0, 2.0], n_trials=7) == 0.0  # 표본 < 3
    assert deflated_sharpe_ratio([1.0] * 30, n_trials=7) == 0.0  # 분산 0


def test_compute_metrics_includes_dsr():
    def trade(net, r_unit=2.0):
        return BTTrade(symbol="005930", entry_time="093500", exit_time="103500",
                       entry_price=10_000.0, exit_price=10_000.0 + net / 10, qty=10,
                       reason="익절1차", gross=net + 300.0, cost=300.0,
                       date="20260105", r_unit_pct=r_unit)

    trades = [trade(3_000.0)] * 20 + [trade(-2_000.0)] * 10
    m = compute_metrics(trades, fallback_r_pct=2.0)
    assert "sharpe_r" in m and m["sharpe_r"] > 0
    assert "deflated_sharpe" in m and 0.0 <= m["deflated_sharpe"] <= 1.0
    m_more_trials = compute_metrics(trades, fallback_r_pct=2.0, n_trials=100)
    assert m_more_trials["deflated_sharpe"] < m["deflated_sharpe"]
