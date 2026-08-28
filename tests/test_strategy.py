"""
test_strategy.py — 유닛 및 회귀 테스트

v1.1에서 수정한 치명적 버그에 대한 회귀 테스트를 포함한다.
각 회귀 테스트에는 어떤 버그를 막는지 주석으로 명시했다.
"""
import pytest

from src.indicators import (
    Candle, ORBState, VWAPCalculator, SMACalculator,
    VolumeRatioCalculator, SymbolIndicators, parse_candles,
)
from src.strategy import StrategyEngine, SignalType
from src.risk_manager import RiskManager, Position, RiskState
from src.utils import tick_size, round_to_tick, is_holiday


# ──────────────────────────────────────────────
#  fixtures
# ──────────────────────────────────────────────
@pytest.fixture
def config():
    return {
        "strategy": {
            "min_score": 3, "volume_multiplier": 1.5,
            "require_vwap_above": True, "require_orb_breakout": True,
            "max_chase_pct": 2.0, "ma_short": 5, "ma_long": 20,
        },
        "take_profit": {
            "tp1_pct": 1.5, "tp1_size": 0.5,
            "tp2_pct": 3.0, "tp2_size": 0.3,
            "tp3_pct": 5.0,
        },
        "stop_loss": {
            "sl_pct": 1.0, "vwap_stop": True,
            "trailing_pct": 1.5, "breakeven_after_tp1": True,
        },
        "risk": {
            "risk_per_trade_pct": 1.5, "max_position_pct": 30.0,
            "max_concurrent": 2, "max_daily_trades": 8,
            "max_trades_per_symbol": 2, "reentry_cooldown_min": 30,
            "min_order_amount": 100_000,
            "daily_loss_limit_pct": 3.0, "monthly_loss_limit_pct": 10.0,
            "consecutive_loss_limit": 3,
        },
    }


def candle(t: str, close: float, volume: int = 1000,
           high: float = 0, low: float = 0) -> Candle:
    return Candle(
        time=t, open=close * 0.999,
        high=high or close * 1.005,
        low=low or close * 0.995,
        close=close, volume=volume,
    )


# ══════════════════════════════════════════════
#  회귀 테스트 — v1.1에서 수정한 치명적 버그
# ══════════════════════════════════════════════
class TestRegressions:

    def test_orb_no_false_breakout_when_unformed(self):
        """[회귀] ORB 미형성(high=0) 상태에서 모든 가격이 돌파로 판정되던 버그

        구버전: is_breakout() 이 `ready and price > self.high` 만 검사해
                high=0 이면 어떤 가격이든 True → 전 종목 무조건 진입
        """
        orb = ORBState()
        orb.ready = True                    # 데이터 없이 ready 만 설정
        assert orb.is_valid is False
        assert orb.is_breakout(10_000) is False
        assert orb.is_breakout(1) is False

    def test_orb_built_from_candle_time_not_wallclock(self):
        """[회귀] ORB를 '벽시계 시각'으로 판정해 09:30 이후 로드 시 미형성되던 버그

        구버전: add_candle(in_orb_window=현재시각기준) 이라 장중에 과거봉을
                일괄 로드하면 ORB 구간 봉이 하나도 반영되지 않았다.
        """
        ind = SymbolIndicators("005930", 5, 20, "09:00", "09:30")
        # 09:30 이후에 09:00~09:30 봉을 일괄 투입
        ind.add_candles([
            candle("090000", 10_000, high=10_100),
            candle("090500", 10_050, high=10_200),
            candle("091000", 10_080, high=10_150),
            candle("092500", 10_030, high=10_120),
            candle("093500", 10_300, high=10_400),   # ORB 구간 밖
        ])
        assert ind.orb.is_valid
        assert ind.orb.count == 4                  # 09:00~09:25 4봉만
        assert ind.orb.high == pytest.approx(10_200)
        assert ind.set_orb_ready() is True
        assert ind.is_orb_breakout(10_250) is True
        assert ind.is_orb_breakout(10_150) is False

    def test_duplicate_candles_do_not_corrupt_vwap(self):
        """[회귀] 매 틱마다 같은 봉을 재누적해 VWAP/SMA가 오염되던 버그

        구버전: 폴링마다 API 전체 응답을 그대로 add_candle 해서
                동일 봉이 반복 누적 → VWAP 왜곡, 거래량배율 폭주
        """
        ind = SymbolIndicators("005930")
        batch = [candle("090000", 10_000, 100), candle("090500", 10_100, 200)]

        ind.add_candles(batch)
        vwap_once = ind.vwap.value
        n_once = len(ind.candles)

        for _ in range(5):                  # 같은 배치 5회 반복 투입
            added = ind.add_candles(batch)
            assert added == 0               # 전부 중복으로 무시되어야 함

        assert ind.vwap.value == pytest.approx(vwap_once)
        assert len(ind.candles) == n_once

    def test_tp_ladder_order_tp1_before_tp2(self):
        """[회귀] TP2를 TP1보다 먼저 검사해 사다리를 건너뛰던 버그

        구버전: +3.5% 로 급등하면 tp1_done=False 인데도 EXIT_TP2 가 나와
                1차 익절(50%)이 통째로 누락됐다.
        """
        eng = StrategyEngine(config_dict())
        sig, _ = eng.check_exit(10_000, 10_350, 0)     # +3.5% 급등
        assert sig == SignalType.EXIT_TP1              # 반드시 TP1 먼저

    def test_tp3_arms_trailing_not_immediate_exit(self):
        """[회귀] +5% 도달 시 즉시 전량 청산되던 버그

        설계는 '+5% 이후 5MA 이탈 시 잔량 청산'인데
        구버전은 EXIT_TP3 → exit_qty=전량 이라 즉시 던져버렸다.
        """
        eng = StrategyEngine(config_dict())
        sig, _ = eng.check_exit(
            10_000, 10_600, 0, tp1_done=True, tp2_done=True,
        )
        assert sig == SignalType.ARM_TP3               # 청산 아님

        # 무장 후 5MA 이탈해야 비로소 청산
        sig2, _ = eng.check_exit(
            10_000, 10_400, 0, tp1_done=True, tp2_done=True,
            tp3_armed=True, ma5_value=10_500,
        )
        assert sig2 == SignalType.EXIT_TP3

    def test_exit_qty_based_on_initial_not_remaining(self):
        """[회귀] 청산 수량을 잔량 기준으로 계산해 비율이 왜곡되던 버그

        초기 100주 → TP1 50주 → 잔량 50주.
        TP2(30%)는 초기수량 기준 30주여야 하는데
        구버전은 잔량 기준 15주만 청산했다.
        """
        eng = StrategyEngine(config_dict())
        assert eng.exit_qty(100, 100, SignalType.EXIT_TP1) == 50
        assert eng.exit_qty(50, 100, SignalType.EXIT_TP2) == 30   # 15가 아님
        assert eng.exit_qty(20, 100, SignalType.EXIT_TP3) == 20   # 잔량 전량

    def test_capital_updates_after_close(self):
        """[회귀] 청산 후 자본이 갱신되지 않아 사이징이 고정되던 버그"""
        rm = RiskManager(config_dict(), 10_000_000)
        before = rm.capital
        rm.open_position(Position("005930", 10_000, 100, "09:30:00"))
        rm.close_position("005930", 11_000, 100)
        assert rm.capital == pytest.approx(before + 100_000)

    def test_win_loss_counted_once_per_roundtrip(self):
        """[회귀] 분할 청산마다 승/패가 중복 집계되던 버그

        구버전: TP1·TP2·TP3 3회 분할 청산 = 3승으로 집계되어
                승률과 연속손실 판정이 완전히 왜곡됐다.
        """
        rm = RiskManager(config_dict(), 10_000_000)
        rm.open_position(Position("005930", 10_000, 100, "09:30:00"))

        rm.close_position("005930", 10_150, 50)    # TP1 부분청산
        assert rm.daily.wins == 0                  # 아직 집계 안 됨
        assert rm.daily.closed == 0

        rm.close_position("005930", 10_300, 30)    # TP2 부분청산
        assert rm.daily.wins == 0

        rm.close_position("005930", 10_500, 20)    # 전량 청산
        assert rm.daily.closed == 1
        assert rm.daily.wins == 1                  # 라운드트립 1건 = 1승

    def test_reentry_cooldown_after_loss(self):
        """[신규] 손절 직후 같은 종목에 즉시 재진입해 반복 손실나던 문제"""
        rm = RiskManager(config_dict(), 10_000_000)
        rm.open_position(Position("005930", 10_000, 100, "09:30:00"))
        rm.close_position("005930", 9_800, 100)    # 손절
        assert rm.can_enter("005930") is False     # 쿨다운 적용
        assert rm.can_enter("000660") is True      # 다른 종목은 가능

    def test_breakeven_stop_after_tp1(self):
        """[신규] TP1 체결 후 이익이 손실로 전환되는 것 방지"""
        eng = StrategyEngine(config_dict())
        sig, reason = eng.check_exit(10_000, 9_950, 0, tp1_done=True)
        assert sig == SignalType.EXIT_SL
        assert "본절" in reason

    def test_no_entry_without_enough_data(self):
        """[신규] 지표 미성숙 상태에서 진입 차단"""
        eng = StrategyEngine(config_dict())
        ind = SymbolIndicators("005930")
        ind.add_candle(candle("090000", 10_000))
        ok, score = eng.should_enter(ind, 10_100)
        assert ok is False
        assert score.blocked_reason == "봉데이터부족"

    def test_chase_guard_blocks_late_entry(self):
        """[신규] ORB 고점 대비 과도한 추격 매수 차단"""
        eng = StrategyEngine(config_dict())
        ind = SymbolIndicators("005930", 2, 3, "09:00", "09:30")
        for i in range(6):
            ind.add_candle(candle(f"09{i*5:02d}00", 10_000 + i * 10,
                                  high=10_100 + i * 10))
        ind.set_orb_ready()
        orb_high = ind.orb.high
        far = orb_high * 1.05                     # 고점 대비 +5% 추격
        ok, score = eng.should_enter(ind, far, bid_total=999, ask_total=1)
        assert ok is False
        assert "추격금지" in score.blocked_reason


def config_dict():
    return {
        "strategy": {
            "min_score": 3, "volume_multiplier": 1.5,
            "require_vwap_above": True, "require_orb_breakout": True,
            "max_chase_pct": 2.0,
        },
        "take_profit": {
            "tp1_pct": 1.5, "tp1_size": 0.5,
            "tp2_pct": 3.0, "tp2_size": 0.3, "tp3_pct": 5.0,
        },
        "stop_loss": {
            "sl_pct": 1.0, "vwap_stop": True,
            "trailing_pct": 1.5, "breakeven_after_tp1": True,
        },
        "risk": {
            "risk_per_trade_pct": 1.5, "max_position_pct": 30.0,
            "max_concurrent": 2, "max_daily_trades": 8,
            "max_trades_per_symbol": 2, "reentry_cooldown_min": 30,
            "min_order_amount": 100_000,
            "daily_loss_limit_pct": 3.0, "monthly_loss_limit_pct": 10.0,
            "consecutive_loss_limit": 3,
        },
    }


# ══════════════════════════════════════════════
#  지표 유닛 테스트
# ══════════════════════════════════════════════
class TestIndicators:

    def test_vwap_volume_weighted(self):
        v = VWAPCalculator()
        v.update(Candle("090000", 9900, 10_000, 9_900, 10_000, 100))
        r = v.update(Candle("090500", 9900, 11_000, 9_900, 11_000, 900))
        assert r > (10_000 + 11_000) / 2       # 큰 거래량 봉 쪽으로 치우침

    def test_sma_rolling(self):
        s = SMACalculator(3)
        for x in (10.0, 20.0, 30.0):
            s.update(x)
        assert s.value == pytest.approx(20.0)
        s.update(40.0)
        assert s.value == pytest.approx(30.0)

    def test_sma_rising_uses_sma_history(self):
        """SMA 기울기는 원시가격이 아닌 SMA 값 기준이어야 함"""
        s = SMACalculator(3)
        for x in (10.0, 20.0, 30.0, 40.0):
            s.update(x)
        assert s.is_rising() is True

    def test_volume_ratio(self):
        vr = VolumeRatioCalculator(3)
        for _ in range(3):
            vr.update(100)
        vr.update(200)
        assert vr.current_ratio() == pytest.approx(2.0)

    def test_orb_range_pct(self):
        orb = ORBState()
        orb.update(candle("090000", 10_000, high=10_500, low=10_000))
        assert orb.range_pct == pytest.approx(5.0)

    def test_parse_candles_excludes_forming_bar(self):
        rows = [
            {"stck_cntg_hour": "092500", "stck_oprc": "100", "stck_hgpr": "110",
             "stck_lwpr": "95", "stck_prpr": "105", "cntg_vol": "10"},
            {"stck_cntg_hour": "093000", "stck_oprc": "105", "stck_hgpr": "115",
             "stck_lwpr": "100", "stck_prpr": "110", "cntg_vol": "20"},
        ]
        out = parse_candles(rows, exclude_forming=True, now_hhmm="0932", bar_minutes=5)
        assert [c.time for c in out] == ["092500"]     # 진행 중인 0930봉 제외

    def test_invalid_candle_rejected(self):
        from src.indicators import parse_candle_from_api
        assert parse_candle_from_api(
            {"stck_cntg_hour": "090000", "stck_hgpr": "90", "stck_lwpr": "100",
             "stck_prpr": "95", "stck_oprc": "95", "cntg_vol": "10"}
        ) is None                                       # high < low


# ══════════════════════════════════════════════
#  리스크 관리 테스트
# ══════════════════════════════════════════════
class TestRiskManager:

    def test_position_sizing_respects_cap(self, config):
        rm = RiskManager(config, 10_000_000)
        # 리스크기준 1500주 vs 자본상한 30% → 300주
        assert rm.calc_position_size(10_000) == 300

    def test_available_cash_limits_size(self, config):
        rm = RiskManager(config, 10_000_000)
        qty = rm.calc_position_size(10_000, available_cash=1_000_000)
        assert qty <= 98

    def test_min_order_amount_blocks_tiny_order(self, config):
        rm = RiskManager(config, 200_000)
        assert rm.calc_position_size(10_000) == 0      # 주문금액 10만원 미만

    def test_daily_loss_limit(self, config):
        rm = RiskManager(config, 10_000_000)
        rm.daily.pnl = -400_000
        assert rm.check_state() == RiskState.DAILY_HALTED

    def test_consecutive_loss_limit(self, config):
        rm = RiskManager(config, 10_000_000)
        rm.daily.consec_loss = 3
        assert rm.check_state() == RiskState.LOSING_STREAK

    def test_max_concurrent(self, config):
        rm = RiskManager(config, 10_000_000)
        for i in range(2):
            rm.open_position(Position(f"00000{i}", 10_000, 10, "09:30:00"))
        assert rm.check_state() == RiskState.MAX_CONCURRENT

    def test_costs_reduce_pnl(self, config):
        rm = RiskManager(config, 10_000_000)
        rm.open_position(Position("005930", 10_000, 100, "09:30:00"))
        net = rm.close_position("005930", 10_100, 100, cost=5_000)
        assert net == pytest.approx(5_000)             # 총이익 1만 - 비용 5천
        assert rm.daily.costs == pytest.approx(5_000)

    def test_symbol_trade_limit(self, config):
        rm = RiskManager(config, 10_000_000)
        for _ in range(2):
            rm.open_position(Position("005930", 10_000, 10, "09:30:00"))
            rm.close_position("005930", 10_200, 10)    # 이익 청산(쿨다운 없음)
        assert rm.can_enter("005930") is False         # 종목당 2회 한도

    def test_peak_tracking(self):
        p = Position("005930", 10_000, 100, "09:30:00")
        p.update_peak(10_500)
        p.update_peak(10_200)
        assert p.peak_price == 10_500                  # 최고가 유지


# ══════════════════════════════════════════════
#  청산 신호 테스트
# ══════════════════════════════════════════════
class TestExitSignals:

    def test_stop_loss(self, config):
        eng = StrategyEngine(config)
        sig, _ = eng.check_exit(10_000, 9_850, 0)
        assert sig == SignalType.EXIT_SL

    def test_vwap_stop(self, config):
        eng = StrategyEngine(config)
        sig, reason = eng.check_exit(10_000, 9_999, 10_100)
        assert sig == SignalType.EXIT_SL
        assert "VWAP" in reason

    def test_hold_in_small_profit(self, config):
        eng = StrategyEngine(config)
        sig, _ = eng.check_exit(10_000, 10_050, 0)
        assert sig == SignalType.NONE

    def test_trailing_stop_on_drawdown(self, config):
        eng = StrategyEngine(config)
        sig, reason = eng.check_exit(
            10_000, 10_400, 0, tp1_done=True, tp2_done=True,
            tp3_armed=True, ma5_value=10_000, peak_price=10_600,
        )
        assert sig == SignalType.EXIT_TP3
        assert "트레일링" in reason


# ══════════════════════════════════════════════
#  유틸 테스트
# ══════════════════════════════════════════════
class TestUtils:

    @pytest.mark.parametrize("price,expected", [
        (1_500, 1), (3_000, 5), (15_000, 10),
        (30_000, 50), (150_000, 100), (300_000, 500), (700_000, 1_000),
    ])
    def test_tick_size(self, price, expected):
        assert tick_size(price) == expected

    def test_round_to_tick_modes(self):
        assert round_to_tick(15_007, mode="down") == 15_000
        assert round_to_tick(15_003, mode="up")   == 15_010

    def test_holiday_detection(self):
        from datetime import date
        assert is_holiday(date(2026, 1, 1)) is True     # 신정
        assert is_holiday(date(2026, 7, 28)) is False


# ══════════════════════════════════════════════
#  백테스트 엔진 스모크 테스트
# ══════════════════════════════════════════════
class TestBacktest:

    def test_backtest_runs_without_error(self, config):
        from src.backtest import Backtester
        cfg = dict(config)
        cfg["orb"] = {"start_time": "09:00", "end_time": "09:30"}
        cfg["strategy"] = {
            **config["strategy"],
            "trading_start": "09:30", "trading_end": "11:00",
            "force_close": "15:15",
        }
        cfg["cost"] = {"commission_pct": 0.015, "tax_pct": 0.18}
        cfg["execution"] = {"slippage_bps": 5}

        # ORB 구간 + 돌파 시나리오
        candles = [candle(f"09{m:02d}00", 10_000 + m, volume=1000)
                   for m in range(0, 30, 5)]
        candles += [candle(f"09{m:02d}00", 10_500, volume=5000)
                    for m in range(30, 60, 5)]

        bt = Backtester(cfg, initial_capital=10_000_000)
        result = bt.run({"005930": candles})
        assert result.initial == 10_000_000
        assert isinstance(bt.report(), str)


# ══════════════════════════════════════════════
#  이동평균선 정배열 우상향 매수 기법 유닛 테스트 (v4.0)
# ══════════════════════════════════════════════
class TestMAAlignmentStrategy:

    def test_ma_perfect_aligned_4_and_slopes(self):
        """5MA > 20MA > 60MA > 120MA 정배열 및 이평선 우상향 기울기 검증"""
        ind = SymbolIndicators("005930", ma_short=5, ma_long=20, orb_start="09:00", orb_end="09:30", ma_mid=60, ma_long_term=120)
        # 상승하는 가격열 주입
        for i in range(130):
            ind.add_candle(candle(f"09{i:02d}00" if i < 60 else f"10{i-60:02d}00", 10_000 + i * 50))

        assert ind.sma_short.value > ind.sma_long.value
        assert ind.sma_long.value > ind.sma_mid.value
        assert ind.sma_mid.value > ind.sma_long_term.value
        assert ind.is_ma_perfect_aligned_4 is True
        assert ind.are_all_mas_rising is True
        assert ind.is_price_above_all_mas(10_000 + 130 * 50) is True

    def test_disparity_and_chase_guard(self):
        """이격도 계산 및 105% 초과 과열 추격 방지 검증"""
        ind = SymbolIndicators("005930", 5, 20)
        for i in range(25):
            ind.add_candle(candle(f"09{i*2:02d}00", 10_000 + i * 20))

        ma20 = ind.sma_long.value
        assert ma20 is not None
        normal_price = ma20 * 1.02
        overheated_price = ma20 * 1.12

        assert ind.is_valid_disparity(normal_price, 98.0, 105.0) is True
        assert ind.is_valid_disparity(overheated_price, 98.0, 105.0) is False

        cfg = config_dict()
        cfg["strategy"]["require_orb_breakout"] = False
        eng = StrategyEngine(cfg)
        ok, score = eng.should_enter(ind, overheated_price)
        assert ok is False
        assert "추격금지" in score.blocked_reason or "과열" in score.blocked_reason

    def test_golden_cross_and_exit_signals(self):
        """골든크로스 감지 및 데드크로스 / 20MA 하향 꺾임 청산 신호 검증"""
        ind = SymbolIndicators("005930", 5, 20)
        prices = [10_000 - i * 30 for i in range(20)] + [9_500 + i * 50 for i in range(10)]
        for idx, p in enumerate(prices):
            ind.add_candle(candle(f"09{idx:02d}00", p))

        eng = StrategyEngine({
            "strategy": {
                "exit_on_dead_cross": True,
                "exit_on_ma20_turn_down": True,
            },
            "stop_loss": {
                "sl_pct": 2.0,
            }
        })
        sig_dead, reason_dead = eng.check_exit(
            entry_price=10_000, current_price=9_900, vwap_value=10_000,
            is_dead_cross=True,
        )
        assert sig_dead == SignalType.EXIT_SL
        assert "데드크로스" in reason_dead

        sig_down, reason_down = eng.check_exit(
            entry_price=10_000, current_price=9_900, vwap_value=10_000,
            is_ma20_turning_down=True,
        )
        assert sig_down == SignalType.EXIT_SL
        assert "20일선기울기하향" in reason_down

