"""StrategyEngine.evaluate_exit 공통 조립 테스트

실전(Executor)과 백테스트가 공유하는 청산 판정 경로가
기존 check_exit 직접 호출과 같은 결론을 내는지 확인한다.
"""
from src.risk_manager import Position
from src.strategy import SignalType, StrategyEngine

CONFIG = {
    "strategy": {"min_score": 5},
    "take_profit": {"mode": "r_multiple", "tp1_r": 2.0, "tp2_r": 3.5,
                    "tp1_size": 0.33, "tp2_size": 0.33},
    "stop_loss": {"mode": "atr", "sl_pct": 1.5, "atr_multiplier": 2.0,
                  "vwap_stop": False},
    "cost": {"commission_pct": 0.015, "tax_pct": 0.2, "slippage_pct": 0.05},
}


def _pos(entry=10_000.0, **kw):
    return Position(symbol="005930", entry_price=entry, qty=10,
                    entry_time="09:35:00", stop_pct=1.5, r_unit_pct=1.8, **kw)


def test_evaluate_exit_stop_loss_without_indicators():
    engine = StrategyEngine(CONFIG)
    signal, _ = engine.evaluate_exit(_pos(), None, 9_800.0, elapsed_min=10.0)
    assert signal == SignalType.EXIT_SL  # -2.0% <= -1.5%(손절폭)


def test_evaluate_exit_no_signal_at_entry_price():
    engine = StrategyEngine(CONFIG)
    signal, _ = engine.evaluate_exit(_pos(), None, 10_000.0, elapsed_min=10.0)
    assert signal == SignalType.NONE


def test_evaluate_exit_matches_direct_check_exit():
    engine = StrategyEngine(CONFIG)
    pos = _pos()
    via_eval = engine.evaluate_exit(pos, None, 10_400.0, elapsed_min=10.0)
    direct = engine.check_exit(
        entry_price=pos.entry_price, current_price=10_400.0, vwap_value=0.0,
        tp1_done=False, tp2_done=False, tp3_armed=False, ma5_value=None,
        peak_price=pos.peak_price, stop_pct=1.5, r_unit_pct=1.8,
    )
    assert via_eval[0] == direct[0]
