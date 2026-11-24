"""ETF 듀얼모멘텀 테스트 — 선택 로직·미래참조 방지·비용 회계·워크포워드 스모크"""
from datetime import date, timedelta

from src.etf_momentum import (
    EtfMomentumBacktester, EtfMomentumConfig, momentum_return,
    monthly_vol_pct, select_targets, target_weights,
)
from src.swing import DailyBar, swing_walk_forward
from src.validation import verdict


# ── 순수 함수 ──

def test_momentum_return():
    closes = [100.0] * 252 + [110.0]
    assert abs(momentum_return(closes, 252, 252) - 0.10) < 1e-9
    assert momentum_return(closes, 100, 252) is None  # 히스토리 부족


def test_select_targets_absolute_filter():
    # 상위 2(A 0.30, C 0.20)가 모두 방어(0.10)를 넘으면 둘 다 선택
    rets = {"A": 0.30, "B": 0.05, "C": 0.20}
    assert select_targets(rets, defensive_return=0.10, top_n=2) == ["A", "C"]
    # 상위권이어도 방어보다 못하면 탈락
    assert select_targets({"A": 0.30, "B": 0.05}, defensive_return=0.10, top_n=2) == ["A"]
    # 전부 방어보다 못하면 빈 리스트 (전액 방어)
    assert select_targets(rets, defensive_return=0.50, top_n=2) == []


def test_target_weights_defensive_fill():
    w = target_weights({"A": 0.30, "B": 0.01}, 0.10, "DEF", top_n=2)
    assert w == {"A": 0.5, "DEF": 0.5}
    w2 = target_weights({"A": -0.01, "B": -0.02}, 0.10, "DEF", top_n=2)
    assert w2 == {"DEF": 1.0}
    assert abs(sum(w.values()) - 1.0) < 1e-9


def test_monthly_vol_pct():
    closes = [100.0] * 61
    assert monthly_vol_pct(closes, 60) == 0.0  # 무변동
    noisy = [100.0 * (1.01 ** i) for i in range(61)]
    assert monthly_vol_pct(noisy, 60) < 1e-9  # 일정 수익률은 표준편차 ~0


# ── 백테스터 합성 데이터 ──

def _bars(symbol_days: dict[str, list[tuple[str, float]]]) -> dict[str, list[DailyBar]]:
    out = {}
    for sym, rows in symbol_days.items():
        out[sym] = [DailyBar(date=d, open=p, high=p, low=p, close=p, volume=1000)
                    for d, p in rows]
    return out


def _date_seq(n: int, start: date = date(2023, 1, 2)) -> list[str]:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return days


def _cfg() -> EtfMomentumConfig:
    return EtfMomentumConfig(risk_symbols=["UP", "DOWN"], defensive_symbol="DEF",
                             lookback_days=60, top_n=2, cost_pct_oneway=0.05,
                             vol_window_days=20)


def _synthetic(n_days: int = 200):
    """UP은 꾸준히 상승, DOWN은 하락, DEF는 거의 고정인 200거래일"""
    dates = _date_seq(n_days)
    up = [(d, 100.0 * (1.002 ** i)) for i, d in enumerate(dates)]
    down = [(d, 100.0 * (0.998 ** i)) for i, d in enumerate(dates)]
    defn = [(d, 100.0 * (1.0001 ** i)) for i, d in enumerate(dates)]
    return _bars({"UP": up, "DOWN": down, "DEF": defn})


def test_backtester_picks_uptrend_and_avoids_downtrend():
    bars = _synthetic()
    bt = EtfMomentumBacktester(bars, _cfg(), {})
    result = bt.run(capital=10_000_000)
    symbols = {t.symbol for t in result["trades"]}
    assert "UP" in symbols
    assert "DOWN" not in symbols  # 절대모멘텀에서 방어에 밀려 선택 안 됨
    assert result["end_capital"] > 10_000_000  # 상승 자산+방어만으로 수익
    assert all(t.reason in ("월간리밸런싱", "기간종료") for t in result["trades"])


def test_backtester_no_trade_before_lookback():
    bars = _synthetic()
    bt = EtfMomentumBacktester(bars, _cfg(), {})
    result = bt.run(capital=10_000_000)
    dates = _date_seq(200)
    # 첫 진입은 룩백(60일) 이후 첫 월말 신호의 다음날부터만 가능
    first_entry = min(t.entry_time for t in result["trades"])
    assert first_entry > dates[60]


def test_backtester_entry_price_is_execution_day_open():
    bars = _synthetic()
    bt = EtfMomentumBacktester(bars, _cfg(), {})
    result = bt.run(capital=10_000_000)
    up_bars = {b.date: b for b in bars["UP"]}
    for t in result["trades"]:
        if t.symbol == "UP":
            assert t.entry_price == up_bars[t.entry_time].open


def test_backtester_cost_accounting_matches_cash():
    """기록된 총비용 ≈ 실제 턴오버 비용 (현금 흐름과 정합)"""
    bars = _synthetic()
    bt = EtfMomentumBacktester(bars, _cfg(), {})
    result = bt.run(capital=10_000_000)
    total_cost = sum(t.cost for t in result["trades"])
    gross_sum = sum(t.gross for t in result["trades"])
    # 자본 변화 = 총손익(gross) - 총비용 과 대략 일치해야 한다 (현금 잔여분 제외 오차 내)
    net_from_trades = gross_sum - total_cost
    capital_change = result["end_capital"] - 10_000_000
    assert abs(net_from_trades - capital_change) < 10_000  # 잔여 현금 반올림 오차 허용


def test_walk_forward_smoke():
    bars = _synthetic(500)
    cfg = EtfMomentumConfig(risk_symbols=["UP", "DOWN"], defensive_symbol="DEF",
                            lookback_days=60, top_n=2, cost_pct_oneway=0.05,
                            vol_window_days=20)
    result = swing_walk_forward(bars, cfg, {}, capital=10_000_000, n_folds=2,
                                warmup_days=60, backtester_cls=EtfMomentumBacktester)
    assert len(result["folds"]) == 2
    assert result["oos_metrics"]["trades"] > 0
    v = verdict(result["oos_metrics"])
    assert v["status"] in ("PASS", "FAIL", "INSUFFICIENT")


def test_segment_r_unit_has_floor():
    """변동성이 거의 없는 구간에서도 1R 하한(0.5%)이 적용돼 R이 폭발하지 않는다"""
    bars = _synthetic()
    bt = EtfMomentumBacktester(bars, _cfg(), {})
    result = bt.run(capital=10_000_000)
    assert result["trades"]
    assert all(t.r_unit_pct >= 0.5 for t in result["trades"])


def test_config_from_yaml_dict():
    cfg = EtfMomentumConfig.from_config({"etf_momentum": {
        "risk_symbols": [69500, "360750"], "defensive_symbol": 153130,
        "lookback_days": 252, "top_n": 2}})
    assert cfg.risk_symbols == ["069500", "360750"]
    assert cfg.defensive_symbol == "153130"
