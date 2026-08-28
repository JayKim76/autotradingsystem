"""test_v2_exits.py — v2.0 청산 로직 검증

구버전(v1.1) 대비 무엇이 달라졌는지를 대조 형태로 검증한다.
각 테스트는 '이 규칙이 없었을 때 어떤 손실이 발생했는가'를 명시한다.
"""
import pytest

from src.strategy import StrategyEngine, SignalType
from src.indicators import SymbolIndicators, Candle, ATRCalculator


# ── 설정 픽스처 ───────────────────────────────────────────────
V2 = {
    "strategy": {"min_score": 2},
    "stop_loss": {
        "mode": "atr", "atr_period": 14, "atr_multiplier": 1.5,
        "sl_pct_min": 1.2, "sl_pct_max": 2.5, "sl_pct": 1.2,
        "vwap_stop": True, "vwap_stop_confirm_bars": 1,
        "trailing_arm_r": 1.0, "trailing_r": 1.0,
        "trailing_ma_exit": False, "breakeven_after_tp1": False,
    },
    "take_profit": {
        "mode": "r_multiple", "include_cost_in_r": True,
        "tp1_r": 1.0, "tp1_size": 0.33, "tp2_r": 2.0, "tp2_size": 0.33,
        "tp1_pct": 1.8, "tp2_pct": 3.4, "tp3_pct": 0,
    },
    "cost": {"commission_pct": 0.015, "tax_pct": 0.20, "slippage_pct": 0.05},
}

V1 = {
    "strategy": {"min_score": 3},
    "stop_loss": {"sl_pct": 1.0, "vwap_stop": True, "trailing_pct": 1.5,
                  "breakeven_after_tp1": True},
    "take_profit": {"tp1_pct": 1.5, "tp1_size": 0.5,
                    "tp2_pct": 3.0, "tp2_size": 0.3, "tp3_pct": 5.0},
    "cost": {"commission_pct": 0.015, "tax_pct": 0.18},
}


def v2():
    return StrategyEngine(V2)


def v1():
    return StrategyEngine(V1)


# ══════════════════════════════════════════════════════════════
#  1. 본절 스톱 — v2.0 최대 변경점
# ══════════════════════════════════════════════════════════════
class TestBreakevenStopRemoved:

    def test_v1_본절스톱이_수익трейд를_0으로_수렴시킨다(self):
        """구버전: TP1 후 진입가로 돌아오면 즉시 전량 청산"""
        eng = v1()
        sig, reason = eng.check_exit(10_000, 9_999, 0, tp1_done=True)
        assert sig == SignalType.EXIT_SL
        assert "본절" in reason

    def test_v2_본절스톱_미발동(self):
        """v2.0: 같은 상황에서 청산하지 않는다 (트레일링이 대체)"""
        eng = v2()
        sig, _ = eng.check_exit(
            10_000, 9_999, 0, tp1_done=True,
            stop_pct=1.5, r_unit_pct=1.83,
        )
        assert sig == SignalType.NONE

    def test_v2_손절선은_여전히_유효(self):
        """본절을 없앴다고 손절까지 없어지면 안 된다"""
        eng = v2()
        sig, reason = eng.check_exit(
            10_000, 9_840, 0, tp1_done=True,     # -1.6%
            stop_pct=1.5, r_unit_pct=1.83,
        )
        assert sig == SignalType.EXIT_SL
        assert "손절" in reason


# ══════════════════════════════════════════════════════════════
#  2. 무방비 구간 (+1.5% ~ +5.0%) 제거
# ══════════════════════════════════════════════════════════════
class TestDeadZoneEliminated:

    def test_v1_45퍼센트에서_되돌리면_본절청산(self):
        """구버전: +4.5%까지 갔다 되돌리면 무장 전이라 본절(0%)로 청산

        +5.0% 무장선에 못 미쳤으므로 트레일링이 걸리지 않았고,
        본절 스톱만 남아 4.5% 수익을 전부 반납했다.
        """
        eng = v1()
        # +4.5% 도달 (미무장 상태) — 아직 청산 신호 없음
        sig, _ = eng.check_exit(10_000, 10_450, 0, tp1_done=True, tp2_done=True,
                                tp3_armed=False, peak_price=10_450)
        assert sig == SignalType.NONE
        # 되돌려 진입가 근처 → 본절 청산 (수익 0)
        sig, reason = eng.check_exit(10_000, 9_990, 0, tp1_done=True, tp2_done=True,
                                     tp3_armed=False, peak_price=10_450)
        assert sig == SignalType.EXIT_SL and "본절" in reason

    def test_v2_같은구간에서_트레일링이_수익을_보존(self):
        """v2.0: +1.0R에서 이미 무장 → 고점 대비 1R 낙폭에 청산"""
        eng = v2()
        r = 1.83                                   # 1R (손절 1.5% + 비용 0.33%)
        # 고점 +4.5% → 1R(1.83%) 이상 되돌림 발생 지점에서 청산
        sig, reason = eng.check_exit(
            10_000, 10_250, 0, tp1_done=True, tp2_done=True,
            tp3_armed=True, peak_price=10_450,
            stop_pct=1.5, r_unit_pct=r,
        )
        assert sig == SignalType.EXIT_TP3
        assert "트레일링" in reason
        # 청산가 +2.5% — 구버전의 0% 대비 수익 보존

    def test_v2_얕은_되돌림에는_버틴다(self):
        eng = v2()
        sig, _ = eng.check_exit(
            10_000, 10_400, 0, tp1_done=True, tp2_done=True,
            tp3_armed=True, peak_price=10_450,     # -0.48% 되돌림
            stop_pct=1.5, r_unit_pct=1.83,
        )
        assert sig == SignalType.NONE


# ══════════════════════════════════════════════════════════════
#  3. 무장 후에도 익절 사다리가 계속된다 (구조 버그 수정)
# ══════════════════════════════════════════════════════════════
class TestLadderContinuesAfterArming:

    def test_수정은_구설정에도_적용된다(self):
        """구버전 버그: tp3_armed 상태면 TP1/TP2를 영영 건너뛰었다

        (구코드는 `if tp3_armed:` 블록 끝에서 무조건 return NONE 했다.)
        이 수정은 모드와 무관한 구조 버그 수정이므로 v1.1 설정에서도
        사다리가 정상 진행되어야 한다.
        """
        eng = v1()
        sig, _ = eng.check_exit(10_000, 10_600, 0, tp1_done=False,
                                tp3_armed=True, peak_price=10_600)
        assert sig == SignalType.EXIT_TP1

    def test_v2_무장후에도_TP1이_체결된다(self):
        eng = v2()
        sig, _ = eng.check_exit(
            10_000, 10_600, 0, tp1_done=False,
            tp3_armed=True, peak_price=10_600,
            stop_pct=1.5, r_unit_pct=1.83,
        )
        assert sig == SignalType.EXIT_TP1

    def test_v2_TP1_다음_TP2_순서유지(self):
        eng = v2()
        sig, _ = eng.check_exit(
            10_000, 10_600, 0, tp1_done=True, tp2_done=False,
            tp3_armed=True, peak_price=10_600,
            stop_pct=1.5, r_unit_pct=1.83,
        )
        assert sig == SignalType.EXIT_TP2   # +6% > 2R(3.66%)


# ══════════════════════════════════════════════════════════════
#  4. R배수 익절 + 비용 내재화
# ══════════════════════════════════════════════════════════════
class TestRMultipleTakeProfit:

    def test_1R은_손절폭에_비용을_더한_값(self):
        eng = v2()
        assert eng.round_trip_cost_pct == pytest.approx(0.33)
        assert eng.resolve_r_unit_pct(1.5) == pytest.approx(1.83)

    def test_비용_미포함시_1R은_손절폭_그대로(self):
        cfg = {**V2, "take_profit": {**V2["take_profit"], "include_cost_in_r": False}}
        assert StrategyEngine(cfg).resolve_r_unit_pct(1.5) == pytest.approx(1.5)

    def test_TP1은_1R_도달시_발동(self):
        eng = v2()
        r = 1.83
        # 1R 직전 — 미발동
        sig, _ = eng.check_exit(10_000, 10_180, 0, stop_pct=1.5, r_unit_pct=r)
        assert sig == SignalType.NONE
        # 1R 도달 — 발동
        sig, _ = eng.check_exit(10_000, 10_184, 0, stop_pct=1.5, r_unit_pct=r)
        assert sig == SignalType.EXIT_TP1

    def test_손절폭이_넓으면_목표가도_비례해서_멀어진다(self):
        """변동성 큰 종목은 목표가도 자동으로 멀어져야 한다"""
        eng = v2()
        narrow = eng.resolve_r_unit_pct(1.2)
        wide   = eng.resolve_r_unit_pct(2.5)
        assert eng._tp_level_pct(1, wide) > eng._tp_level_pct(1, narrow)

    def test_익절수량은_초기수량_33퍼센트(self):
        eng = v2()
        assert eng.exit_qty(100, 100, SignalType.EXIT_TP1) == 33
        assert eng.exit_qty(67, 100, SignalType.EXIT_TP2) == 33
        assert eng.exit_qty(34, 100, SignalType.EXIT_TP3) == 34   # 잔량 전량


# ══════════════════════════════════════════════════════════════
#  5. ATR 기반 손절폭
# ══════════════════════════════════════════════════════════════
class TestATRStop:

    def test_ATR배수가_손절폭이_된다(self):
        eng = v2()
        assert eng.resolve_stop_pct(1.0) == pytest.approx(1.5)    # 1.0 × 1.5

    def test_하한_클램프(self):
        """ATR이 작아도 sl_pct_min 아래로는 좁히지 않는다"""
        assert v2().resolve_stop_pct(0.2) == pytest.approx(1.2)

    def test_상한_클램프(self):
        """ATR이 커도 sl_pct_max 위로는 넓히지 않는다"""
        assert v2().resolve_stop_pct(5.0) == pytest.approx(2.5)

    def test_ATR_미성숙시_폴백(self):
        assert v2().resolve_stop_pct(None) == pytest.approx(1.2)

    def test_fixed_모드는_ATR을_무시(self):
        cfg = {**V2, "stop_loss": {**V2["stop_loss"], "mode": "fixed"}}
        assert StrategyEngine(cfg).resolve_stop_pct(3.0) == pytest.approx(1.2)

    def test_ATR계산_정확성(self):
        """TR = max(고-저, |고-전종가|, |저-전종가|)"""
        atr = ATRCalculator(period=2)
        atr.update(Candle("090100", 100, 110, 90, 100, 1000))   # TR=20
        atr.update(Candle("090200", 100, 120, 110, 115, 1000))  # TR=max(10,20,10)=20
        assert atr.value == pytest.approx(20.0)
        assert atr.pct == pytest.approx(20 / 115 * 100)

    def test_period_미달시_None(self):
        atr = ATRCalculator(period=5)
        atr.update(Candle("090100", 100, 110, 90, 100, 1000))
        assert atr.value is None


# ══════════════════════════════════════════════════════════════
#  6. VWAP 종가 확정 이탈 (휩쏘 방지)
# ══════════════════════════════════════════════════════════════
class TestVWAPConfirmation:

    def _ind(self):
        return SymbolIndicators("005930", ma_short=2, ma_long=3, atr_period=2)

    def test_확정봉_종가가_VWAP_아래면_이탈(self):
        ind = self._ind()
        # 저가 봉을 넣어 VWAP을 끌어내린 뒤, 마지막 봉 종가가 그 아래로
        ind.add_candle(Candle("090100", 100, 100, 100, 100, 1000))
        ind.add_candle(Candle("090200", 90, 90, 80, 80, 5000))
        assert ind.closed_bars_below_vwap(1) is True

    def test_종가가_VWAP_위면_이탈아님(self):
        ind = self._ind()
        ind.add_candle(Candle("090100", 100, 100, 100, 100, 1000))
        ind.add_candle(Candle("090200", 110, 120, 110, 120, 5000))
        assert ind.closed_bars_below_vwap(1) is False

    def test_봉이_부족하면_이탈아님(self):
        assert self._ind().closed_bars_below_vwap(2) is False

    def test_틱_순간이탈은_확정판정을_이기지_못한다(self):
        """v2.0 핵심: 현재가가 VWAP 아래여도 봉 종가가 확정되지 않았으면 청산 안 함"""
        eng = v2()
        sig, _ = eng.check_exit(
            10_000, 9_950, vwap_value=10_100,      # 틱 기준으로는 이탈
            stop_pct=1.5, r_unit_pct=1.83,
            vwap_breached=False,                   # 확정 이탈 아님
        )
        assert sig == SignalType.NONE

    def test_확정이탈이면_청산(self):
        eng = v2()
        sig, reason = eng.check_exit(
            10_000, 9_950, vwap_value=10_100,
            stop_pct=1.5, r_unit_pct=1.83,
            vwap_breached=True,
        )
        assert sig == SignalType.EXIT_SL and "VWAP" in reason

    def test_구버전은_틱기준으로_판정(self):
        """vwap_breached 미전달 시 하위호환 동작"""
        eng = v2()
        sig, _ = eng.check_exit(10_000, 9_950, vwap_value=10_100,
                                stop_pct=1.5, r_unit_pct=1.83)
        assert sig == SignalType.EXIT_SL


# ══════════════════════════════════════════════════════════════
#  7. 트레일링 무장 시점
# ══════════════════════════════════════════════════════════════
class TestTrailingArm:

    def test_1R_도달시_무장(self):
        eng = v2()
        sig, _ = eng.check_exit(
            10_000, 10_184, 0, tp1_done=True, tp2_done=True,
            stop_pct=1.5, r_unit_pct=1.83,
        )
        assert sig == SignalType.ARM_TP3

    def test_1R_미만이면_무장안함(self):
        eng = v2()
        sig, _ = eng.check_exit(
            10_000, 10_100, 0, tp1_done=True, tp2_done=True,
            stop_pct=1.5, r_unit_pct=1.83,
        )
        assert sig == SignalType.NONE

    def test_구버전은_5퍼센트에서_무장(self):
        eng = v1()
        sig, _ = eng.check_exit(10_000, 10_450, 0, tp1_done=True, tp2_done=True)
        assert sig == SignalType.NONE          # +4.5% — 아직
        sig, _ = eng.check_exit(10_000, 10_510, 0, tp1_done=True, tp2_done=True)
        assert sig == SignalType.ARM_TP3       # +5.1%

    def test_5MA이탈_기본_비활성(self):
        """무장이 앞당겨진 만큼 1분봉 5MA 이탈은 과도하게 타이트하다"""
        eng = v2()
        sig, _ = eng.check_exit(
            10_000, 10_300, 0, tp1_done=True, tp2_done=True,
            tp3_armed=True, ma5_value=10_400,      # 5MA 아래
            peak_price=10_350,
            stop_pct=1.5, r_unit_pct=1.83,
        )
        assert sig == SignalType.NONE

    def test_5MA이탈_명시적_활성화_가능(self):
        cfg = {**V2, "stop_loss": {**V2["stop_loss"], "trailing_ma_exit": True}}
        sig, reason = StrategyEngine(cfg).check_exit(
            10_000, 10_300, 0, tp1_done=True, tp2_done=True,
            tp3_armed=True, ma5_value=10_400, peak_price=10_350,
            stop_pct=1.5, r_unit_pct=1.83,
        )
        assert sig == SignalType.EXIT_TP3 and "5MA" in reason


# ══════════════════════════════════════════════════════════════
#  8. 우선순위 — 손절이 항상 최우선
# ══════════════════════════════════════════════════════════════
class TestPriority:

    def test_손절이_익절보다_우선(self):
        eng = v2()
        sig, reason = eng.check_exit(
            10_000, 9_800, 0, stop_pct=1.5, r_unit_pct=1.83,
        )
        assert sig == SignalType.EXIT_SL

    def test_포지션별_손절폭이_config를_덮어쓴다(self):
        """ATR로 확정된 stop_pct가 우선 적용되어야 한다"""
        eng = v2()
        # -1.3%: config sl_pct(1.2) 기준이면 손절이지만 stop_pct=2.5면 아니다
        sig, _ = eng.check_exit(10_000, 9_870, 0, stop_pct=2.5, r_unit_pct=2.83)
        assert sig == SignalType.NONE
        sig, _ = eng.check_exit(10_000, 9_870, 0, stop_pct=1.2, r_unit_pct=1.53)
        assert sig == SignalType.EXIT_SL


# ══════════════════════════════════════════════════════════════
#  9. 기대값 시뮬레이션 — v1 vs v2 대조
# ══════════════════════════════════════════════════════════════
class TestExpectancy:

    def _simulate(self, eng, path, stop_pct, r_unit, sizes):
        """가격 경로를 따라 청산 이벤트를 재생하고 실현 수익률(%)을 반환

        sizes: (tp1_size, tp2_size) — 초기수량 대비 비중
        """
        entry = 10_000.0
        remaining, peak, realized = 1.0, entry, 0.0
        tp1 = tp2 = armed = False

        for price in path:
            if remaining <= 0:
                break
            peak = max(peak, price)
            sig, _ = eng.check_exit(
                entry, price, vwap_value=0,
                tp1_done=tp1, tp2_done=tp2, tp3_armed=armed,
                peak_price=peak, stop_pct=stop_pct, r_unit_pct=r_unit,
            )
            pnl = (price - entry) / entry * 100

            if sig == SignalType.ARM_TP3:
                armed = True
            elif sig == SignalType.EXIT_TP1:
                realized += sizes[0] * pnl; remaining -= sizes[0]; tp1 = True
            elif sig == SignalType.EXIT_TP2:
                realized += sizes[1] * pnl; remaining -= sizes[1]; tp2 = True
            elif sig in (SignalType.EXIT_TP3, SignalType.EXIT_SL):
                realized += remaining * pnl; remaining = 0

        if remaining > 0:                       # 15:15 강제청산
            realized += remaining * (path[-1] - entry) / entry * 100
        return realized

    def test_큰수익_경로에서_v2가_v1을_능가한다(self):
        """+5%까지 갔다가 진입가로 되돌아오는 경로

        구버전은 본절 스톱 때문에 수익 대부분을 반납한다.
        """
        path = [10_050, 10_200, 10_350, 10_500, 10_400, 10_200,
                10_100, 10_000, 9_990]

        v1_pnl = self._simulate(v1(), path, stop_pct=1.0, r_unit=1.0, sizes=(0.5, 0.3))
        v2_pnl = self._simulate(v2(), path, stop_pct=1.5, r_unit=1.83, sizes=(0.33, 0.33))

        print(f"\n  [큰수익 경로] v1.1={v1_pnl:+.2f}%  v2.0={v2_pnl:+.2f}%")
        assert v2_pnl > v1_pnl

    def test_무방비구간_경로에서_격차가_가장_크다(self):
        """+4.5%까지만 갔다 되돌아오는 경로 — v2.0의 핵심 개선 지점

        구버전은 +5.0% 무장선에 닿지 못해 트레일링이 걸리지 않고,
        본절 스톱만 남아 4.5% 수익을 통째로 반납한다.
        """
        path = [10_050, 10_200, 10_350, 10_450, 10_300, 10_150,
                10_050, 10_000, 9_995]

        v1_pnl = self._simulate(v1(), path, stop_pct=1.0, r_unit=1.0, sizes=(0.5, 0.3))
        v2_pnl = self._simulate(v2(), path, stop_pct=1.5, r_unit=1.83, sizes=(0.33, 0.33))

        print(f"\n  [무방비구간]  v1.1={v1_pnl:+.2f}%  v2.0={v2_pnl:+.2f}%")
        assert v2_pnl > v1_pnl

    def test_손실_경로에서는_v2가_더_넓게_손절(self):
        """손절폭을 넓힌 대가는 분명히 존재한다 — 트레이드오프를 명시"""
        path = [9_950, 9_900, 9_870, 9_840, 9_800]

        v1_pnl = self._simulate(v1(), path, stop_pct=1.0, r_unit=1.0, sizes=(0.5, 0.3))
        v2_pnl = self._simulate(v2(), path, stop_pct=1.5, r_unit=1.83, sizes=(0.33, 0.33))

        print(f"\n  [손실 경로]   v1.1={v1_pnl:+.2f}%  v2.0={v2_pnl:+.2f}%")
        assert v2_pnl < v1_pnl        # v2는 더 크게 잃는다 (의도된 트레이드오프)
        assert v2_pnl > -2.0          # 다만 sl_pct_max 안에서 통제된다


class TestDayLowSupportProtection:
    def test_day_low_rebound_defers_stop_loss(self):
        """당일 저점 부근에서 반등 양봉 형성 중일 경우 순간 틱 털림 방지"""
        eng = v2()
        # pnl -1.3% (sl_pct = 1.2%), day_low = 9,870, current = 9,870 (당일 저점 지지선 터치)
        sig, reason = eng.check_exit(
            entry_price=10_000,
            current_price=9_870,
            vwap_value=10_000,
            stop_pct=1.2,
            day_low=9_870,
            is_rebound=True,
            closed_below_day_low=False,
        )
        # 반등 캔들 형성 중이므로 틱 털림(손절 직후 급등) 방지 유예
        assert sig == SignalType.NONE

    def test_day_low_breakdown_executes_stop_loss(self):
        """당일 저점이 종가 또는 명확히 붕괴(current < day_low * 0.997)되면 손절 실행"""
        eng = v2()
        sig, reason = eng.check_exit(
            entry_price=10_000,
            current_price=9_800,
            vwap_value=10_000,
            stop_pct=1.2,
            day_low=9_870,
            is_rebound=False,
            closed_below_day_low=True,
        )
        assert sig == SignalType.EXIT_SL
        assert "손절" in reason

