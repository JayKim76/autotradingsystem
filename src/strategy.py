"""
strategy.py — 5-체크리스트 스코어링 엔진

v3.0 수정사항 (눌림목 매수 전략 전환)
  - [핵심] 돌파 매수 → 눌림목 매수 전환
    · ORB 돌파 → 상승 추세 판정 (가격 > 20MA, 20MA 상승)
    · VWAP 위 필수 → VWAP 근접 눌림 (VWAP 위 0~1%)
    · 추격 매수 방지 → 고점 대비 최소 눌림폭 확인
    · 거래량 확증 → 눌림 시 거래량 감소 (매도 압력 약화)
  - 청산 로직(익절/손절/트레일링)은 변경 없음

v2.0 수정사항 (기대값 개선)
  - [핵심] 본절 스톱 기본 비활성화 — 큰 수익 트레이드를 0으로 수렴시키던 원인.
           +1.5%~+5.0% 구간이 무방비였던 문제를 조기 트레일링으로 대체
  - [핵심] 트레일링 무장 시점을 +5.0% 고정 → +1.0R 로 앞당김
  - [핵심] 무장 후에도 익절 사다리가 계속 진행되도록 재구조화
           (구버전은 tp3_armed 상태에서 TP1/TP2를 건너뛰었다)
  - ATR 기반 손절폭 지원 (stop_pct 인자로 포지션별 주입)
  - R배수 익절 — 손절폭에 비례한 목표가로 종목 변동성에 자동 적응
  - 왕복 거래비용을 R 단위에 내재화 (include_cost_in_r)
  - VWAP 이탈을 봉 종가 확정 기준으로 전환 (휩쏘 방지)
  - 5MA 이탈 트레일링을 선택적으로 변경 (trailing_ma_exit)
    → 무장이 +1.0R 로 앞당겨진 만큼 1분봉 5MA 이탈은 과도하게 타이트하다

v1.1 수정사항
  - [치명] TP2가 TP1보다 먼저 평가되어 사다리가 건너뛰어지던 순서 오류 수정
  - [치명] +5% 도달 시 즉시 전량 청산되던 문제 → 트리거 설정 후 5MA 트레일링으로 분리
  - 청산 수량을 '초기 수량' 기준으로 계산 (잔량 기준이면 비율이 왜곡됨)
  - 최고가 추적 기반 트레일링 스톱 추가
  - ORB 미형성 종목 진입 차단
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from .indicators import SymbolIndicators

logger = logging.getLogger("autotrader.strategy")


class SignalType(Enum):
    NONE        = auto()
    ENTER       = auto()
    EXIT_TP1    = auto()
    EXIT_TP2    = auto()
    EXIT_TP3    = auto()   # 잔량 청산 (5MA 이탈 또는 트레일링)
    ARM_TP3     = auto()   # +5% 도달 — 청산 아님, 트레일링 무장만
    EXIT_SL     = auto()
    EXIT_FORCE  = auto()


class ScaleInSignal(Enum):
    """v2.1 추가매수 신호"""
    NONE         = auto()
    PYRAMID_ADD  = auto()   # 수익 중 추가매수
    AVG_DOWN     = auto()   # 하락 시 물타기


@dataclass
class ScoreResult:
    """Pass/Fail 필수 검증 및 100점 만점 이동평균 정배열 매매 체크리스트 (v4.0)"""
    # ── 필수 Pass/Fail 항목 ──
    pass_uptrend:      bool = False    # 상승 추세 (20MA 우상향 & 주가 > 20MA)
    pass_support:      bool = False    # 지지선 영역 도달 (VWAP, 5MA, 또는 20MA)
    pass_dip:          bool = False    # 당일 고점 대비 눌림폭 (-1.0% 이상)
    pass_rebound:      bool = False    # 반등 확정 캔들 또는 골든크로스 신호
    pass_disparity:    bool = True     # 이격도 적정 범위 (20일선 대비 +10% 이내 과열 방지)
    pass_vol_decrease: bool = True     # 눌림목 시 거래량 감축 확인 (매물 소화 확인)

    # ── 이동평균선 정배열 우상향 5대 가산점 (항목당 20점 만점 = 총 100점) ──
    score_ma_aligned:    int = 0    # 1. 이동평균 정배열 (5>20>60>120 20점, 5>20 15점)
    score_ma_rising:     int = 0    # 2. 이평선 우상향 기울기 (전 이평선 상승 20점, 20MA 상승 15점)
    score_price_settled: int = 0    # 3. 주가 이평선 위 안착 & 적정 이격 (20점)
    score_timing:        int = 0    # 4. 매수 타이밍 (눌림목 지지반등 또는 골든크로스 20점)
    score_auxiliary:     int = 0    # 5. 보조지표 (거래량 감축/확증 + RSI/MACD 우상향 20점)

    # 레거시 5점 체크리스트 지원 (하위 호환성)
    ask_dominant:   bool = False    # 매도 잔량 > 매수 잔량 (상방 벽 소화/반등 수급)
    vol_decreasing: bool = False    # 눌림 시 거래량 감축 (건강한 조정)
    ma_aligned:     bool = False    # 이평선 정배열 (5MA > 20MA)
    rsi_rebound:    bool = False    # 1분봉 RSI 30~50 구간 (과매도 탈출 반등)
    near_vwap:      bool = False    # VWAP 0.5% 이내 초밀착 지지

    blocked_reason: str  = ""
    dip_pct:        float = 0.0

    @property
    def total_score_100(self) -> int:
        """100점 만점 스코어"""
        return (
            self.score_ma_aligned
            + self.score_ma_rising
            + self.score_price_settled
            + self.score_timing
            + self.score_auxiliary
        )

    @property
    def is_pass_all(self) -> bool:
        """필수 조건을 모두 통과하였는가"""
        return (
            self.pass_uptrend and self.pass_support and self.pass_dip
            and self.pass_rebound and self.pass_disparity and self.pass_vol_decrease
        )

    @property
    def score(self) -> int:
        """가산점 스코어 (min_score <= 5인 경우 레거시 및 5점 스케일 자동 호환)"""
        if self.total_score_100 > 0:
            return max(1, round(self.total_score_100 / 20.0))
        return sum([
            self.ask_dominant, self.vol_decreasing, self.ma_aligned,
            self.rsi_rebound, self.near_vwap,
        ])

    def __str__(self) -> str:
        req_flags = f"필수=[추세:{'O' if self.pass_uptrend else 'X'} 지지:{'O' if self.pass_support else 'X'} 눌림:{'O' if self.pass_dip else 'X'} 반등:{'O' if self.pass_rebound else 'X'} 이격:{'O' if self.pass_disparity else 'X'} 음량:{'O' if self.pass_vol_decrease else 'X'}]"
        bonus_flags = f"정배열점수={self.total_score_100}/100[정배열:{self.score_ma_aligned}점 기울기:{self.score_ma_rising}점 안착:{self.score_price_settled}점 타이밍:{self.score_timing}점 보조:{self.score_auxiliary}점]"
        suffix = f" | 차단:{self.blocked_reason}" if self.blocked_reason else ""
        return f"{req_flags} {bonus_flags} (눌림{self.dip_pct:.1f}%){suffix}"


class StrategyEngine:
    """100점 스코어링 & 정배열 우상향 전략 엔진"""

    def __init__(self, config: dict) -> None:
        strat  = config.get("strategy", {})
        tp_cfg = config.get("take_profit", {})
        sl_cfg = config.get("stop_loss", {})
        cost_cfg = config.get("cost", {})

        self.min_score:         int   = int(strat.get("min_score", 3))
        self.volume_multiplier: float = float(strat.get("volume_multiplier", 1.5))
        self.require_vwap:      bool  = bool(strat.get("require_vwap_above", True))
        self.require_orb:       bool  = bool(strat.get("require_orb_breakout", True))
        self.max_chase_pct:     float = float(strat.get("max_chase_pct", 2.0))

        # ── 이동평균 정배열 우상향 매수 파라미터 (v4.0) ────────────
        self.entry_timing_mode: str   = str(strat.get("entry_timing_mode", "both"))  # "pullback", "golden_cross", "both"
        self.min_disparity_pct: float = float(strat.get("min_disparity_pct", 98.0))
        self.max_disparity_pct: float = float(strat.get("max_disparity_pct", 110.0))  # 20일선 대비 +10% 제한
        self.require_vol_decrease_pullback: bool = bool(strat.get("require_vol_decrease_pullback", True))
        self.exit_on_ma20_turn_down: bool = bool(strat.get("exit_on_ma20_turn_down", False))
        self.exit_on_dead_cross: bool     = bool(strat.get("exit_on_dead_cross", False))

        # ── 눌림목 매수 파라미터 (v3.0) ─────────────────────────
        self.pullback_vwap_pct:      float = float(strat.get("pullback_vwap_pct", 1.0))
        self.pullback_ma5_pct:       float = float(strat.get("pullback_ma5_pct", 0.5))
        self.min_dip_from_high_pct:  float = float(strat.get("min_dip_from_high_pct", 1.5))
        self.require_uptrend:        bool  = bool(strat.get("require_uptrend", True))
        self.require_pullback:       bool  = bool(strat.get("require_pullback", True))

        # ── 익절 레벨 ─────────────────────────────────────────
        self.tp_mode:  str   = str(tp_cfg.get("mode", "fixed_pct"))
        self.tp1_pct:  float = float(tp_cfg.get("tp1_pct",  1.5))
        self.tp1_size: float = float(tp_cfg.get("tp1_size", 0.5))
        self.tp2_pct:  float = float(tp_cfg.get("tp2_pct",  3.0))
        self.tp2_size: float = float(tp_cfg.get("tp2_size", 0.3))
        self.tp3_pct:  float = float(tp_cfg.get("tp3_pct",  5.0))
        self.tp1_r:    float = float(tp_cfg.get("tp1_r", 1.0))
        self.tp2_r:    float = float(tp_cfg.get("tp2_r", 2.0))
        self.include_cost_in_r: bool = bool(tp_cfg.get("include_cost_in_r", False))

        # ── 손절 / 트레일링 / 휩쏘방지 ───────────────────────
        self.sl_mode:       str   = str(sl_cfg.get("mode", "fixed"))
        self.sl_pct:        float = float(sl_cfg.get("sl_pct", 1.0))
        self.sl_pct_min:    float = float(sl_cfg.get("sl_pct_min", self.sl_pct))
        self.sl_pct_max:    float = float(sl_cfg.get("sl_pct_max", self.sl_pct * 2))
        self.atr_multiplier: float = float(sl_cfg.get("atr_multiplier", 1.5))
        self.vwap_stop:     bool  = bool(sl_cfg.get("vwap_stop", True))
        self.vwap_confirm_bars: int = int(sl_cfg.get("vwap_stop_confirm_bars", 0) or 0)
        self.trailing_pct:  float = float(sl_cfg.get("trailing_pct", 1.5))
        self.breakeven_after_tp1: bool = bool(sl_cfg.get("breakeven_after_tp1", True))
        self.rsi_panic_threshold: float = float(sl_cfg.get("rsi_panic_threshold", 25.0))
        self.panic_volume_ratio: float = float(sl_cfg.get("panic_volume_ratio", 2.0))
        self.market_panic_deferral: bool = bool(sl_cfg.get("market_panic_deferral", True))

        _arm_r = sl_cfg.get("trailing_arm_r")
        self.trailing_arm_r: Optional[float] = float(_arm_r) if _arm_r is not None else None
        _tr_r = sl_cfg.get("trailing_r")
        self.trailing_r: Optional[float] = float(_tr_r) if _tr_r is not None else None

        default_ma_exit = self.trailing_arm_r is None
        self.trailing_ma_exit: bool = bool(sl_cfg.get("trailing_ma_exit", default_ma_exit))

        # ── 왕복 거래비용 (%) ─────────────────────────────────
        self.round_trip_cost_pct: float = (
            float(cost_cfg.get("commission_pct", 0.015)) * 2
            + float(cost_cfg.get("tax_pct", 0.20))
            + float(cost_cfg.get("slippage_pct", 0.0)) * 2
        )

        # ── 추가매수 / 물타기 설정 (v4.0 고도화) ─────────────────────────
        si_cfg = config.get("scale_in", {})
        self.si_enabled:       bool  = bool(si_cfg.get("enabled", False))
        self.si_add1_r:        float = float(si_cfg.get("add1_trigger_r", 1.0))
        self.si_add2_r:        float = float(si_cfg.get("add2_trigger_r", 2.0))
        self.si_add1_size:     float = float(si_cfg.get("add1_size_ratio", 0.5))
        self.si_add2_size:     float = float(si_cfg.get("add2_size_ratio", 0.25))
        self.si_min_score:     int   = int(si_cfg.get("min_score", 3))
        self.si_require_vwap:  bool  = bool(si_cfg.get("require_vwap_above", True))
        self.si_require_ma:    bool  = bool(si_cfg.get("require_ma_aligned", True))

        # 물타기 (무분별한 물타기 방지)
        self.si_avg_down_enabled: bool  = bool(si_cfg.get("avg_down_enabled", False))
        self.si_avg_down_r:      float = float(si_cfg.get("avg_down_trigger_r", -0.5))
        self.si_avg_down_size:   float = min(0.5, float(si_cfg.get("avg_down_size_ratio", 0.5)))  # 최대 50% 이내
        self.si_avg_down_max:    int   = min(1, int(si_cfg.get("avg_down_max", 1)))               # 최대 1회 제한
        self.si_avg_down_require_support: bool = bool(si_cfg.get("avg_down_require_support", True))  # 60MA/VWAP 지지선 필수


    # ── R 단위 산출 (v2.0) ────────────────────────────────────
    def resolve_stop_pct(self, atr_pct: Optional[float] = None) -> float:
        """진입 시점의 손절폭 (%)

        mode="atr" 이면 ATR × 배수를 [sl_pct_min, sl_pct_max] 로 클램프한다.
        ATR이 아직 성숙하지 않았으면 sl_pct 로 폴백한다.
        """
        if self.sl_mode != "atr" or atr_pct is None or atr_pct <= 0:
            return self.sl_pct
        width = atr_pct * self.atr_multiplier
        return min(max(width, self.sl_pct_min), self.sl_pct_max)

    def resolve_r_unit_pct(self, stop_pct: float) -> float:
        """1R 의 크기 (%)

        include_cost_in_r 이면 왕복 비용을 R 에 포함한다.
        이렇게 하면 '+1.0R 익절'의 실현 수익이 정확히 손절폭과 같아져,
        명목 목표가와 실현 손익의 괴리가 사라진다.
        """
        base = stop_pct if stop_pct > 0 else self.sl_pct
        if self.include_cost_in_r:
            base += self.round_trip_cost_pct
        return base

    # ── 내부: 레벨 계산 ───────────────────────────────────────
    def _tp_level_pct(self, stage: int, r_unit: float) -> float:
        """익절 목표 수익률 (%)"""
        if self.tp_mode == "r_multiple" and r_unit > 0:
            return (self.tp1_r if stage == 1 else self.tp2_r) * r_unit
        return self.tp1_pct if stage == 1 else self.tp2_pct

    def _trailing_drop_pct(self, r_unit: float) -> float:
        """트레일링 허용 낙폭 (%)"""
        if self.trailing_r is not None and r_unit > 0:
            return self.trailing_r * r_unit
        return self.trailing_pct

    def _arm_level_pct(self, r_unit: float) -> Optional[float]:
        """트레일링 무장 임계 수익률 (%). None 이면 무장 없음"""
        if self.trailing_arm_r is not None and r_unit > 0:
            return self.trailing_arm_r * r_unit
        return self.tp3_pct if self.tp3_pct > 0 else None

    # ── 진입 스코어 계산 (v4.0 정배열 우상향 100점 체계) ─────────────
    def score_entry(
        self,
        indicators: SymbolIndicators,
        current_price: float,
        ask_total: int = 0,
        bid_total: int = 0,
    ) -> ScoreResult:
        result = ScoreResult()

        if not indicators.has_enough_data:
            result.blocked_reason = "봉데이터부족"
            return result

        # ── 1. Pass/Fail 필수 조건 판정 ──────────────────────────
        result.pass_uptrend = indicators.is_in_uptrend()
        result.pass_support = indicators.is_support_pullback(current_price)
        result.dip_pct = indicators.dip_from_high_pct(current_price)
        result.pass_dip = (result.dip_pct >= self.min_dip_from_high_pct)
        result.pass_disparity = indicators.is_valid_disparity(
            current_price, self.min_disparity_pct, self.max_disparity_pct
        )
        if self.require_vol_decrease_pullback:
            result.pass_vol_decrease = indicators.is_volume_decreasing()
        else:
            result.pass_vol_decrease = True

        is_pullback = indicators.is_support_pullback(current_price)
        is_rebound = indicators.is_rebound_candle()
        is_golden = indicators.is_golden_cross_5_20

        if self.entry_timing_mode == "golden_cross":
            result.pass_rebound = is_golden and is_rebound
        elif self.entry_timing_mode == "pullback":
            result.pass_rebound = is_rebound
        else:  # "both"
            result.pass_rebound = is_rebound or (is_golden and is_rebound)

        # ── 2. 100점 만점 5대 항목 평가 (항목당 20점) ─────────────
        # 1) 이동평균 정배열 (20점)
        if indicators.is_ma_perfect_aligned_4:
            result.score_ma_aligned = 20
        elif indicators.is_ma_perfect_aligned or indicators.is_ma_aligned:
            result.score_ma_aligned = 15
        else:
            result.score_ma_aligned = 0

        # 2) 이평선 우상향 기울기 (20점)
        if indicators.are_all_mas_rising:
            result.score_ma_rising = 20
        elif indicators.sma_long.is_rising():
            result.score_ma_rising = 15
        else:
            result.score_ma_rising = 0

        # 3) 주가 안착 & 이격도 적정 (20점)
        if result.pass_disparity and indicators.is_price_above_all_mas(current_price):
            result.score_price_settled = 20
        elif result.pass_disparity and current_price >= (indicators.sma_long.value or 0):
            result.score_price_settled = 15
        else:
            result.score_price_settled = 0

        # 4) 매수 타이밍 (눌림/골든 20점)
        if is_golden and is_rebound:
            result.score_timing = 20
        elif is_golden or (is_pullback and is_rebound):
            result.score_timing = 20
        elif is_pullback or is_rebound:
            result.score_timing = 10

        # 5) 보조지표 (수급/강도 20점)
        aux_score = 0
        if ask_total > 0 or bid_total > 0:
            result.ask_dominant = (ask_total > bid_total)
        else:
            result.ask_dominant = True
        result.vol_decreasing = indicators.is_volume_decreasing()
        result.ma_aligned = indicators.is_ma_aligned

        rsi_v = indicators.rsi_value
        if rsi_v is not None:
            result.rsi_rebound = (30.0 <= rsi_v <= 55.0)

        vwap_v = indicators.vwap.value
        if vwap_v > 0 and current_price > 0:
            result.near_vwap = (0 <= (current_price - vwap_v) / vwap_v * 100 <= 0.5)

        if result.vol_decreasing or indicators.volume_ratio >= self.volume_multiplier:
            aux_score += 10
        if result.rsi_rebound or indicators.macd.is_bullish():
            aux_score += 10
        result.score_auxiliary = aux_score

        return result

    def should_enter(
        self,
        indicators: SymbolIndicators,
        current_price: float,
        ask_total: int = 0,
        bid_total: int = 0,
    ) -> tuple[bool, ScoreResult]:
        """진입 여부 판단 (v4.0 정배열 우상향 매수 기법)"""
        score = self.score_entry(indicators, current_price, ask_total, bid_total)

        if score.blocked_reason:
            return False, score

        if self.require_orb:
            if not indicators.is_orb_breakout(current_price):
                score.blocked_reason = "ORB고점미돌파"
                return False, score
            if indicators.orb.high > 0:
                chase_pct = (current_price - indicators.orb.high) / indicators.orb.high * 100
                if chase_pct > self.max_chase_pct:
                    score.blocked_reason = f"추격금지({chase_pct:.1f}%>{self.max_chase_pct:.1f}%)"
                    return False, score

        # 필수 조건 검사 (1개라도 미달 시 즉시 차단)
        if not score.pass_uptrend:
            score.blocked_reason = "상승추세미달"
            return False, score
        if not score.pass_disparity:
            disp = indicators.disparity_ma20(current_price)
            score.blocked_reason = f"추격금지(이격도과열{disp:.1f}%)"
            return False, score
        if self.require_vol_decrease_pullback and not score.pass_vol_decrease:
            score.blocked_reason = "눌림시거래량증가(매물소화미확인)"
            return False, score
        if not score.pass_support:
            score.blocked_reason = "지지선미달"
            return False, score
        if not score.pass_dip:
            score.blocked_reason = f"눌림폭미달({score.dip_pct:.1f}%<{self.min_dip_from_high_pct:.1f}%)"
            return False, score
        if not score.pass_rebound:
            score.blocked_reason = "반등확정봉없음(칼날매수방지)"
            return False, score

        # 가산점 최소 스코어 검사
        target_score = self.min_score if self.min_score <= 5 else self.min_score
        current_eval_score = score.score if self.min_score <= 5 else score.total_score_100

        if current_eval_score >= target_score:
            logger.info("[%s] ★ 이동평균 정배열 매수 진입 확정 %s", indicators.symbol, score)
            return True, score

        score.blocked_reason = f"가산점부족({current_eval_score}<{target_score})"
        return False, score

    # ── 청산 조건 판단 ────────────────────────────────────────
    def check_exit(
        self,
        entry_price: float,
        current_price: float,
        vwap_value: float,
        tp1_done: bool = False,
        tp2_done: bool = False,
        tp3_armed: bool = False,
        ma5_value: Optional[float] = None,
        peak_price: Optional[float] = None,
        stop_pct: Optional[float] = None,
        r_unit_pct: float = 0.0,
        vwap_breached: Optional[bool] = None,
        is_dead_cross: bool = False,
        is_ma20_turning_down: bool = False,
        closed_below_ma20: bool = False,
        day_low: Optional[float] = None,
        is_rebound: bool = False,
        closed_below_day_low: bool = False,
        is_market_panic: bool = False,
        rsi_value: Optional[float] = None,
        is_panic_dump: bool = False,
        closed_below_support: Optional[bool] = None,
    ) -> tuple[SignalType, str]:
        """청산 신호 반환

        Returns:
            (신호, 사유 문자열)
        """
        if entry_price <= 0:
            return SignalType.NONE, ""

        pnl_pct = (current_price - entry_price) / entry_price * 100

        sl     = stop_pct if (stop_pct and stop_pct > 0) else self.sl_pct
        r_unit = r_unit_pct if r_unit_pct > 0 else sl

        # ── 1. 손절 (최우선) 및 당일 저가(day_low) 지지 검증 ──
        if pnl_pct <= -sl:
            # ★ 1-1. 시장 전체 폭락/패닉 시 손절 유보 (개별종목 악재가 아닐 가능성 높음)
            if is_market_panic and self.market_panic_deferral and pnl_pct > -sl * 1.5:
                logger.info(
                    "손절 유예 — 시장 동반 급락/패닉 (pnl=%.2f%%, sl=%.2f%%)",
                    pnl_pct, sl
                )
                return SignalType.NONE, "시장전체동반급락_손절유보"

            # ★ 1-2. 당일 투매(패닉 세일) 및 RSI 25 이하 극단적 과매도 보호
            effective_rsi = rsi_value if rsi_value is not None else 100.0
            if (is_panic_dump or effective_rsi <= self.rsi_panic_threshold) and pnl_pct > -sl * 1.5:
                if closed_below_support is not True:
                    rsi_str = f"{effective_rsi:.1f}" if rsi_value is not None else "N/A"
                    logger.info(
                        "손절 유예 — 당일 투매/극단과매도(RSI=%s) 종가 마감 유보 (pnl=%.2f%%)",
                        rsi_str, pnl_pct
                    )
                    return SignalType.NONE, f"투매/RSI극단과매도_종가유보(RSI={rsi_str})"

            # ★ 1-3. 1분봉 종가 마감 이탈 확정 전 틱 순간 휩쏘 보호
            if closed_below_support is False and pnl_pct > -sl * 1.3:
                logger.info(
                    "손절 유예 — 1분봉 종가 마감 이탈 미확정 휩쏘 보호 (pnl=%.2f%%)",
                    pnl_pct
                )
                return SignalType.NONE, "종가마감이탈미확정_유보"

            # ★ 1-4. 당일 저가 지지선 근처 반등 보호 (손절 직후 급등 털림 방지)
            if day_low and day_low > 0 and current_price >= day_low * 0.997:
                if is_rebound and not closed_below_day_low and pnl_pct > -sl * 1.5:
                    logger.info(
                        "손절 유예 — 당일 저가(%.0f) 지지 반등 중 (pnl=%.2f%%, sl=%.2f%%)",
                        day_low, pnl_pct, sl,
                    )
                    return SignalType.NONE, "당일저가지지유예"
                else:
                    return SignalType.EXIT_SL, f"손절({pnl_pct:.2f}% / -{sl:.2f}%)"
            else:
                return SignalType.EXIT_SL, f"손절({pnl_pct:.2f}% / -{sl:.2f}%)"

        # ── 2. 이동평균선 매도 / 손절 조건 ────────────────────
        if self.exit_on_dead_cross and is_dead_cross:
            return SignalType.EXIT_SL, f"5/20데드크로스({pnl_pct:+.2f}%)"
        if self.exit_on_ma20_turn_down and is_ma20_turning_down:
            return SignalType.EXIT_SL, f"20일선기울기하향({pnl_pct:+.2f}%)"
        if closed_below_ma20:
            return SignalType.EXIT_SL, f"20일선확정이탈({pnl_pct:+.2f}%)"

        # ── 3. VWAP 이탈 ──────────────────────────────────────
        if self.vwap_stop:
            breached = (
                vwap_breached
                if vwap_breached is not None
                else (vwap_value > 0 and current_price < vwap_value)
            )
            if breached:
                return SignalType.EXIT_SL, f"VWAP이탈({pnl_pct:+.2f}%)"

        # ── 3. 본절 스톱 (v2.0 기본 비활성) ───────────────────
        if self.breakeven_after_tp1 and tp1_done and pnl_pct < 0:
            return SignalType.EXIT_SL, f"본절스톱({pnl_pct:+.2f}%)"

        # ── 4. 트레일링 청산 (무장 상태) ──────────────────────
        if tp3_armed:
            if self.trailing_ma_exit and ma5_value is not None and current_price < ma5_value:
                return SignalType.EXIT_TP3, f"5MA이탈({pnl_pct:+.2f}%)"
            if peak_price and peak_price > 0:
                drawdown = (peak_price - current_price) / peak_price * 100
                drop_limit = self._trailing_drop_pct(r_unit)
                if drawdown >= drop_limit:
                    return SignalType.EXIT_TP3, f"트레일링({drawdown:.2f}%↓ / {drop_limit:.2f}%)"

        # ── 5. 익절 사다리 — 반드시 낮은 단계부터 순서대로 ────
        if not tp1_done:
            level = self._tp_level_pct(1, r_unit)
            if pnl_pct >= level:
                return SignalType.EXIT_TP1, f"TP1(+{pnl_pct:.2f}% / +{level:.2f}%)"
        elif not tp2_done:
            level = self._tp_level_pct(2, r_unit)
            if pnl_pct >= level:
                return SignalType.EXIT_TP2, f"TP2(+{pnl_pct:.2f}% / +{level:.2f}%)"

        # ── 6. 트레일링 무장 (청산이 아니라 추적 시작) ────────
        if not tp3_armed:
            arm = self._arm_level_pct(r_unit)
            if arm is not None and pnl_pct >= arm:
                return SignalType.ARM_TP3, f"트레일링무장(+{pnl_pct:.2f}% / +{arm:.2f}%)"

        return SignalType.NONE, ""

    # ── 청산 수량 계산 ────────────────────────────────────────
    def exit_qty(
        self,
        remaining_qty: int,
        initial_qty: int,
        signal: SignalType,
    ) -> int:
        if remaining_qty <= 0:
            return 0
        base = initial_qty if initial_qty > 0 else remaining_qty

        if signal == SignalType.EXIT_TP1:
            return min(remaining_qty, max(1, int(base * self.tp1_size)))

        if signal == SignalType.EXIT_TP2:
            return min(remaining_qty, max(1, int(base * self.tp2_size)))

        if signal in (SignalType.EXIT_TP3, SignalType.EXIT_SL, SignalType.EXIT_FORCE):
            return remaining_qty      # 잔량 전량

        return 0

    # ── 추가매수 신호 판정 (v4.0 물타기 차단 고도화) ───────────────────────
    def should_add(
        self,
        indicators: SymbolIndicators,
        current_price: float,
        entry_price: float,
        r_unit_pct: float,
        add_count: int,
        avg_down_count: int,
        ask_total: int = 0,
        bid_total: int = 0,
    ) -> tuple[ScaleInSignal, float, str]:
        """추가매수 신호 판정

        Returns:
            (ScaleInSignal, 추가매수 수량 비율, 사유 문자열)
        """
        if not self.si_enabled or entry_price <= 0 or r_unit_pct <= 0:
            return ScaleInSignal.NONE, 0.0, ""

        pnl_pct = (current_price - entry_price) / entry_price * 100
        pnl_r = pnl_pct / r_unit_pct if r_unit_pct > 0 else 0.0

        # ── 수익 중 피라미딩 ─────────────────────────────
        if pnl_r > 0:
            trigger_r = None
            size_ratio = 0.0

            if add_count == 0 and pnl_r >= self.si_add1_r:
                trigger_r = self.si_add1_r
                size_ratio = self.si_add1_size
            elif add_count == 1 and pnl_r >= self.si_add2_r:
                trigger_r = self.si_add2_r
                size_ratio = self.si_add2_size

            if trigger_r is not None and size_ratio > 0:
                if not self._check_add_conditions(indicators, current_price, ask_total, bid_total):
                    return ScaleInSignal.NONE, 0.0, f"피라미딩 조건 불충족 (+{pnl_r:.1f}R)"

                reason = f"피라미딩{add_count+1}차 (+{pnl_r:.1f}R / +{trigger_r:.1f}R)"
                return ScaleInSignal.PYRAMID_ADD, size_ratio, reason

        # ── 하락 시 물타기 (무조건 물타기 방지) ──────────────────────────────
        if self.si_avg_down_enabled and pnl_r < 0:
            if avg_down_count >= self.si_avg_down_max:
                return ScaleInSignal.NONE, 0.0, f"물타기 횟수 초과 ({avg_down_count}>={self.si_avg_down_max})"

            # 1. 최초 매수 논리(상승 추세 및 정배열) 훼손 여부 확인
            if not indicators.is_in_uptrend() or not indicators.is_ma_aligned:
                return ScaleInSignal.NONE, 0.0, f"최초매수논리훼손(상승추세/정배열이탈 {pnl_r:.1f}R)"

            # 2. 주요 지지선 (60MA, 20MA, 또는 VWAP) 인근 확인
            if self.si_avg_down_require_support:
                is_near_support = (
                    indicators.is_pullback_to_ma60(current_price)
                    or indicators.is_pullback_to_ma20(current_price)
                    or indicators.is_pullback_to_vwap(current_price)
                )
                if not is_near_support:
                    return ScaleInSignal.NONE, 0.0, f"주요지지선미달(60MA/VWAP인근만허용 {pnl_r:.1f}R)"

            target_trigger_r = self.si_avg_down_r * (avg_down_count + 1)
            if pnl_r <= target_trigger_r:
                if not self._check_add_conditions(
                    indicators, current_price, ask_total, bid_total, lenient=True
                ):
                    return ScaleInSignal.NONE, 0.0, f"물타기 조건 불충족 ({pnl_r:.1f}R)"

                size_ratio = min(0.5, self.si_avg_down_size)
                reason = f"물타기{avg_down_count+1}차 ({pnl_r:.1f}R / {target_trigger_r:.1f}R)"
                return ScaleInSignal.AVG_DOWN, size_ratio, reason

        return ScaleInSignal.NONE, 0.0, ""


    def _check_add_conditions(
        self,
        indicators: SymbolIndicators,
        current_price: float,
        ask_total: int = 0,
        bid_total: int = 0,
        lenient: bool = False,
    ) -> bool:
        """추가매수 조건 체크

        lenient=True 이면 물타기용 완화된 기준 (스코어 2점 + VWAP 참고)
        """
        if not indicators.has_enough_data:
            return False

        score = self.score_entry(indicators, current_price, ask_total, bid_total)

        min_score = self.si_min_score if not lenient else max(2, self.si_min_score - 1)
        if score.score < min_score:
            return False

        # 상승 추세 필수 (물타기는 완화)
        if self.si_require_vwap and not lenient:
            if not score.pass_uptrend:
                return False

        # 이평선 정배열 필수 (물타기는 완화)
        if self.si_require_ma and not lenient:
            if not score.ma_aligned:
                return False

        return True
