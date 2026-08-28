"""
test_whipsaw_prevention.py — 휩쏘 함정 방지 매수·물타기·손절 규칙 유닛 테스트
"""
import pytest
from src.indicators import SymbolIndicators, Candle
from src.strategy import StrategyEngine, SignalType, ScaleInSignal


def candle(t: str, close: float, volume: int = 1000, high: float = 0, low: float = 0) -> Candle:
    return Candle(
        time=t,
        open=close * 0.999,
        high=high or close * 1.005,
        low=low or close * 0.995,
        close=close,
        volume=volume,
    )


@pytest.fixture
def strategy_config():
    return {
        "strategy": {
            "min_score": 3,
            "volume_multiplier": 1.5,
            "require_vwap_above": False,
            "require_orb_breakout": False,
            "max_chase_pct": 2.0,
            "ma_short": 5,
            "ma_long": 20,
            "ma_mid": 60,
            "ma_long_term": 120,
            "min_disparity_pct": 98.0,
            "max_disparity_pct": 110.0,
            "require_vol_decrease_pullback": True,
            "min_dip_from_high_pct": 1.0,
        },
        "take_profit": {
            "mode": "r_multiple",
            "tp1_r": 1.0,
            "tp1_size": 0.33,
            "tp2_r": 2.0,
            "tp2_size": 0.33,
        },
        "stop_loss": {
            "mode": "atr",
            "sl_pct": 1.5,
            "sl_pct_min": 1.5,
            "sl_pct_max": 2.5,
            "atr_multiplier": 1.5,
            "vwap_stop": False,
            "rsi_panic_threshold": 25.0,
            "panic_volume_ratio": 2.0,
            "market_panic_deferral": True,
        },
        "scale_in": {
            "enabled": True,
            "avg_down_enabled": True,
            "avg_down_trigger_r": -0.5,
            "avg_down_max": 1,
            "avg_down_size_ratio": 0.5,
            "avg_down_require_support": True,
            "min_score": 2,
        },
    }


class TestWhipsawPrevention:

    def test_high_disparity_chase_blocked(self, strategy_config):
        """20일선 대비 +10% 초과 이격도 과열 진입 차단 테스트"""
        strat = StrategyEngine(strategy_config)
        ind = SymbolIndicators("005930", 5, 20, "09:00", "09:30")
        
        # 20MA가 약 10,240원에 우상향 형성되도록 봉 투입
        for i in range(25):
            ind.add_candle(candle(f"09{i:02d}00", 10_000 + i * 20, volume=1000))

        ma20 = ind.sma_long.value
        assert ma20 is not None
        overheated_price = ma20 * 1.15  # 이격도 115% > 110%

        score = strat.score_entry(ind, overheated_price)
        assert score.pass_disparity is False
        
        ok, score_res = strat.should_enter(ind, overheated_price)
        assert ok is False
        assert "추격금지(이격도과열" in score_res.blocked_reason

    def test_volume_increase_pullback_blocked(self, strategy_config):
        """눌림목 시 거래량 증가 (매물 소화 미확인) 진입 차단 테스트"""
        strat = StrategyEngine(strategy_config)
        ind = SymbolIndicators("005930", 5, 20, "09:00", "09:30")

        # 거래량 1000으로 기본 형성
        for i in range(20):
            ind.add_candle(candle(f"09{i:02d}00", 10_000 + i * 10, volume=1000))

        # 최신 봉에서 거래량이 5000으로 폭증하며 음봉/눌림
        ind.add_candle(candle("092100", 10_150, volume=5000))

        score = strat.score_entry(ind, 10_150)
        assert score.pass_vol_decrease is False
        
        ok, score_res = strat.should_enter(ind, 10_150)
        assert ok is False
        assert "눌림시거래량증가" in score_res.blocked_reason

    def test_avg_down_blocked_when_logic_broken_or_no_support(self, strategy_config):
        """상승추세/정배열 훼손 시 또는 60MA 지지선 미달 시 물타기 차단 테스트"""
        strat = StrategyEngine(strategy_config)
        ind = SymbolIndicators("005930", 5, 20, "09:00", "09:30")

        for i in range(25):
            ind.add_candle(candle(f"09{i:02d}00", 10_000 - i * 10, volume=1000))

        # 상승 추세 이탈 상태 (20MA 하향 중 & 주가 < 20MA)
        sig, size, reason = strat.should_add(
            indicators=ind,
            current_price=9_700,
            entry_price=10_000,
            r_unit_pct=1.5,
            add_count=0,
            avg_down_count=0,
        )
        assert sig == ScaleInSignal.NONE
        assert "최초매수논리훼손" in reason

    def test_avg_down_max_count_limit(self, strategy_config):
        """물타기 횟수 1회 초과 시 차단 테스트"""
        strat = StrategyEngine(strategy_config)
        ind = SymbolIndicators("005930", 5, 20, "09:00", "09:30")

        for i in range(25):
            ind.add_candle(candle(f"09{i:02d}00", 10_000 + i * 10, volume=1000))

        # avg_down_count=1 (이미 1회 실행함)
        sig, size, reason = strat.should_add(
            indicators=ind,
            current_price=10_000,
            entry_price=10_300,
            r_unit_pct=1.5,
            add_count=0,
            avg_down_count=1,
        )
        assert sig == ScaleInSignal.NONE
        assert "물타기 횟수 초과" in reason

    def test_stop_loss_market_panic_deferral(self, strategy_config):
        """시장 전체 동반 폭락 시 손절 유보 테스트"""
        strat = StrategyEngine(strategy_config)

        # 진입가 10,000원, 현재가 9,800원 (-2.0% 하락, sl=1.5% 경과)
        # 시장 패닉 (is_market_panic=True)
        sig, reason = strat.check_exit(
            entry_price=10_000,
            current_price=9_800,
            vwap_value=9_900,
            stop_pct=1.5,
            is_market_panic=True,
        )
        assert sig == SignalType.NONE
        assert "시장전체동반급락_손절유보" in reason

    def test_stop_loss_panic_dump_and_rsi_deferral(self, strategy_config):
        """당일 200% 거래량 폭증 투매 또는 RSI 25 이하 진입 시 종가 확정 전 손절 유보 테스트"""
        strat = StrategyEngine(strategy_config)

        # 1) 투매 (is_panic_dump=True)
        sig, reason = strat.check_exit(
            entry_price=10_000,
            current_price=9_800,
            vwap_value=9_900,
            stop_pct=1.5,
            is_panic_dump=True,
            closed_below_support=False,
        )
        assert sig == SignalType.NONE
        assert "투매/RSI극단과매도_종가유보" in reason

        # 2) RSI 20 (rsi_value <= 25)
        sig2, reason2 = strat.check_exit(
            entry_price=10_000,
            current_price=9_800,
            vwap_value=9_900,
            stop_pct=1.5,
            rsi_value=20.0,
            closed_below_support=False,
        )
        assert sig2 == SignalType.NONE
        assert "투매/RSI극단과매도_종가유보" in reason2

    def test_stop_loss_bar_close_unconfirmed_deferral(self, strategy_config):
        """1분봉 종가 지지선 이탈 미확정 (틱 순간 이탈) 휩쏘 보호 테스트"""
        strat = StrategyEngine(strategy_config)

        # -1.8% 하락 (sl=1.5%), 종가 이탈 미확정 (closed_below_support=False)
        sig, reason = strat.check_exit(
            entry_price=10_000,
            current_price=9_820,
            vwap_value=9_900,
            stop_pct=1.5,
            closed_below_support=False,
        )
        assert sig == SignalType.NONE
        assert "종가마감이탈미확정_유보" in reason
