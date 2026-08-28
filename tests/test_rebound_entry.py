import pytest
from src.indicators import SymbolIndicators, Candle
from src.strategy import StrategyEngine


def test_is_rebound_candle_pinbar():
    ind = SymbolIndicators("002990")
    # 음봉이지만 아랫꼬리가 길게 달린 망치형 지지 캔들 (High=100, Low=80, Open=95, Close=92)
    # high_low = 20, (close - low) = 12, pos_ratio = 12/20 = 0.6 >= 0.5 -> True
    c = Candle(time="100000", open=95.0, high=100.0, low=80.0, close=92.0, volume=1000)
    ind.candles.append(c)
    assert ind.is_rebound_candle() is True


def test_entry_blocked_on_red_falling_candle():
    config = {
        "strategy": {
            "entry_timing_mode": "both",
            "require_vwap_above": False,
            "require_orb_breakout": False,
            "min_dip_from_high_pct": 0.0,
            "min_score": 1,
        },
        "stop_loss": {"mode": "atr", "sl_pct_min": 1.5},
        "take_profit": {},
    }
    engine = StrategyEngine(config)
    ind = SymbolIndicators("002990")

    # 20봉 생성하여 상승 추세 및 골든 크로스 상태 만들기 (5MA > 20MA)
    for i in range(25):
        price = 100.0 + i * 0.5
        c = Candle(time=f"09{i:02d}00", open=price, high=price+1, low=price-1, close=price+0.5, volume=1000)
        ind.add_candle(c)

    # 마지막 봉이 음봉 하락인 경우 (open=115, close=110)
    red_candle = Candle(time="092600", open=115.0, high=116.0, low=109.0, close=110.0, volume=1000)
    ind.candles[-1] = red_candle

    # 골든크로스 상태라도 캔들이 음봉 하락 중이면 is_rebound_candle이 False이므로 score_entry의 pass_rebound는 False여야 함
    score = engine.score_entry(ind, current_price=110.0)
    assert score.pass_rebound is False

    should, res = engine.should_enter(ind, current_price=110.0)
    assert should is False
    assert res.blocked_reason in ("반등확정봉없음(칼날매수방지)", "지지선미달")
