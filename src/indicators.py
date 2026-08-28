"""
indicators.py — VWAP / SMA / ORB / 거래량 배율 지표 계산

v1.1 수정사항
  - [치명] 동일 봉 중복 누적 방지 (봉 시각 기준 dedup)
  - [치명] ORB 구간 판정을 '벽시계 시각' → '봉 자체 시각' 기준으로 변경
  - 미완성(현재 진행 중) 봉 제외 옵션
  - 봉 히스토리 메모리 상한
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("autotrader.indicators")

MAX_CANDLE_HISTORY = 500     # 봉 보관 상한 (메모리 누수 방지)


def _hhmm(t: str) -> str:
    """'HHMMSS' 또는 'HH:MM' → 'HHMM' 정규화"""
    t = t.replace(":", "")
    return t[:4].rjust(4, "0")


@dataclass
class Candle:
    """N분봉 하나"""
    time:   str    # 'HHMMSS' (봉 시작 시각)
    open:   float
    high:   float
    low:    float
    close:  float
    volume: int

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def hhmm(self) -> str:
        return _hhmm(self.time)


@dataclass
class ORBState:
    """ORB (Opening Range Breakout) 구간 상태"""
    high:   float = 0.0
    low:    float = float("inf")
    ready:  bool  = False          # ORB 관찰 완료 여부
    count:  int   = 0              # 누적된 ORB 봉 개수
    candles: list[Candle] = field(default_factory=list)

    def update(self, candle: Candle) -> None:
        self.high = max(self.high, candle.high)
        self.low  = min(self.low,  candle.low)
        self.count += 1
        self.candles.append(candle)

    @property
    def is_valid(self) -> bool:
        """ORB가 실제 데이터로 형성되었는지 — 최소 1봉 이상 + 고점 유효"""
        return self.count > 0 and self.high > 0

    def is_breakout(self, price: float) -> bool:
        """ORB 고점 돌파 여부

        주의: ORB가 형성되지 않았으면(count=0, high=0) 항상 False를 반환해야 한다.
              이전 버전은 high=0 상태에서 모든 가격이 돌파로 판정되는 버그가 있었다.
        """
        if not self.ready or not self.is_valid:
            return False
        return price > self.high

    @property
    def range_pct(self) -> float:
        """ORB 밴드 폭 (%) — 변동성 필터용"""
        if not self.is_valid or self.low == float("inf") or self.low <= 0:
            return 0.0
        return (self.high - self.low) / self.low * 100


# ──────────────────────────────────────────────
#  VWAP 계산기
# ──────────────────────────────────────────────
class VWAPCalculator:
    """당일 누적 VWAP"""

    def __init__(self) -> None:
        self._cum_pv: float = 0.0
        self._cum_vol: float = 0.0

    def reset(self) -> None:
        self._cum_pv  = 0.0
        self._cum_vol = 0.0

    def update(self, candle: Candle) -> float:
        self._cum_pv  += candle.typical_price * candle.volume
        self._cum_vol += candle.volume
        return self.value

    @property
    def value(self) -> float:
        if self._cum_vol == 0:
            return 0.0
        return self._cum_pv / self._cum_vol


# ──────────────────────────────────────────────
#  SMA (단순 이동평균)
# ──────────────────────────────────────────────
class SMACalculator:
    """고정 창 SMA + 기울기 추적"""

    def __init__(self, period: int) -> None:
        self.period = int(period)
        self._prices: list[float] = []
        self._history: list[float] = []     # SMA 값 이력 (기울기 판정용)

    def update(self, price: float) -> Optional[float]:
        self._prices.append(price)
        if len(self._prices) > self.period:
            self._prices.pop(0)

        v = self.value
        if v is not None:
            self._history.append(v)
            if len(self._history) > 10:
                self._history.pop(0)
        return v

    @property
    def value(self) -> Optional[float]:
        if len(self._prices) < self.period:
            return None
        return sum(self._prices) / self.period

    def is_rising(self) -> bool:
        """SMA 값 자체가 우상향인지 (원시 가격이 아닌 SMA 기울기 기준)

        이전 버전은 원시 가격 2개를 비교해 노이즈에 취약했다.
        """
        if len(self._history) < 2:
            return False
        return self._history[-1] > self._history[-2]

    def slope_pct(self) -> float:
        """최근 SMA 기울기 (%)"""
        if len(self._history) < 2 or self._history[-2] == 0:
            return 0.0
        return (self._history[-1] - self._history[-2]) / self._history[-2] * 100


# ──────────────────────────────────────────────
#  거래량 배율 계산
# ──────────────────────────────────────────────
class VolumeRatioCalculator:
    """최신봉 거래량 / 직전 N봉 평균 거래량"""

    def __init__(self, lookback: int = 10) -> None:
        self.lookback = lookback
        self._volumes: list[int] = []

    def update(self, volume: int) -> None:
        self._volumes.append(volume)
        if len(self._volumes) > self.lookback + 1:
            self._volumes.pop(0)

    def current_ratio(self) -> float:
        if len(self._volumes) < 2:
            return 0.0
        prev = self._volumes[:-1]
        avg = sum(prev) / len(prev)
        if avg <= 0:
            return 0.0
        return self._volumes[-1] / avg


# ──────────────────────────────────────────────
#  ATR (Average True Range)  — v2.0 신규
# ──────────────────────────────────────────────
class ATRCalculator:
    """평균 실질 변동폭

    고정 % 손절은 종목별 변동성을 무시한다. 코스닥 모멘텀 종목의
    1분봉 ATR은 그 자체로 0.5~1.0% 에 달해, 고정 1.0% 손절은
    방향이 맞아도 노이즈에 잘려나간다. ATR 배수로 손절폭을 잡으면
    종목 변동성에 자동으로 적응한다.

    True Range = max(고-저, |고-전일종가|, |저-전일종가|)
    """

    def __init__(self, period: int = 14) -> None:
        self.period = max(1, int(period))
        self._trs: list[float] = []
        self._prev_close: Optional[float] = None
        self._last_close: float = 0.0

    def update(self, candle: Candle) -> Optional[float]:
        if self._prev_close is None:
            tr = candle.high - candle.low
        else:
            tr = max(
                candle.high - candle.low,
                abs(candle.high - self._prev_close),
                abs(candle.low - self._prev_close),
            )
        self._trs.append(max(0.0, tr))
        if len(self._trs) > self.period:
            self._trs.pop(0)

        self._prev_close = candle.close
        self._last_close = candle.close
        return self.value

    @property
    def value(self) -> Optional[float]:
        """ATR 절대값 (원). 봉이 period 만큼 쌓이기 전에는 None"""
        if len(self._trs) < self.period:
            return None
        return sum(self._trs) / len(self._trs)

    @property
    def pct(self) -> Optional[float]:
        """종가 대비 ATR 비율 (%)"""
        atr = self.value
        if atr is None or self._last_close <= 0:
            return None
        return atr / self._last_close * 100


# ──────────────────────────────────────────────
#  RSI (상대강도지수) 계산기
# ──────────────────────────────────────────────
class RSICalculator:
    """RSI 계산기 (14봉)"""

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self._closes: list[float] = []

    def update(self, close: float) -> Optional[float]:
        self._closes.append(close)
        if len(self._closes) > self.period + 1:
            self._closes.pop(0)
        return self.value

    @property
    def value(self) -> Optional[float]:
        if len(self._closes) < self.period + 1:
            return None
        gains = []
        losses = []
        for i in range(1, len(self._closes)):
            diff = self._closes[i] - self._closes[i - 1]
            if diff >= 0:
                gains.append(diff)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(diff))
        avg_gain = sum(gains) / self.period
        avg_loss = sum(losses) / self.period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))


# ──────────────────────────────────────────────
#  MACD (이동평균 수렴·확산) 계산기
# ──────────────────────────────────────────────
class MACDCalculator:
    """MACD (12, 26, 9) 계산기"""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        self.fast = fast
        self.slow = slow
        self.signal = signal
        self._closes: list[float] = []
        self.macd: Optional[float] = None
        self.signal_line: Optional[float] = None
        self.hist: Optional[float] = None
        self._macd_history: list[float] = []
        self._signal_history: list[float] = []

    def update(self, close: float) -> tuple[Optional[float], Optional[float], Optional[float]]:
        self._closes.append(close)
        if len(self._closes) < self.slow:
            return None, None, None

        def _ema(prices: list[float], period: int) -> float:
            k = 2.0 / (period + 1)
            ema = sum(prices[:period]) / period
            for p in prices[period:]:
                ema = (p * k) + (ema * (1 - k))
            return ema

        fast_ema = _ema(self._closes, self.fast)
        slow_ema = _ema(self._closes, self.slow)
        self.macd = fast_ema - slow_ema
        self._macd_history.append(self.macd)
        if len(self._macd_history) > MAX_CANDLE_HISTORY:
            self._macd_history.pop(0)

        if len(self._macd_history) >= self.signal:
            self.signal_line = _ema(self._macd_history, self.signal)
            self._signal_history.append(self.signal_line)
            if len(self._signal_history) > MAX_CANDLE_HISTORY:
                self._signal_history.pop(0)
            self.hist = self.macd - self.signal_line

        return self.macd, self.signal_line, self.hist

    def is_bullish(self) -> bool:
        """MACD > Signal 라인 또는 Signal 우상향"""
        if self.macd is None or self.signal_line is None:
            return False
        if self.macd > self.signal_line:
            return True
        if len(self._signal_history) >= 2 and self._signal_history[-1] > self._signal_history[-2]:
            return True
        return False

    def is_golden_cross(self) -> bool:
        """MACD가 Signal 라인을 골든크로스"""
        if len(self._macd_history) < 2 or len(self._signal_history) < 2:
            return False
        prev_m, prev_s = self._macd_history[-2], self._signal_history[-2]
        curr_m, curr_s = self._macd_history[-1], self._signal_history[-1]
        return prev_m <= prev_s and curr_m > curr_s


# ──────────────────────────────────────────────
#  종목별 통합 지표 관리자
# ──────────────────────────────────────────────
class SymbolIndicators:
    """종목 하나의 모든 지표를 통합 관리

    ORB 구간 판정은 봉의 시각(candle.time)으로 수행하므로,
    장중 어느 시점에 과거 봉을 일괄 로드해도 ORB가 정확히 재구성된다.
    """

    def __init__(
        self,
        symbol: str,
        ma_short: int = 5,
        ma_long: int = 20,
        orb_start: str = "09:00",
        orb_end:   str = "09:30",
        vol_lookback: int = 10,
        atr_period: int = 14,
        ma_mid: int = 60,
        ma_long_term: int = 120,
    ) -> None:
        self.symbol         = symbol
        self.orb_start      = _hhmm(str(orb_start))
        self.orb_end        = _hhmm(str(orb_end))

        self.orb            = ORBState()
        self.vwap           = VWAPCalculator()
        self.sma_short      = SMACalculator(ma_short)
        self.sma_long       = SMACalculator(ma_long)
        self.sma_mid        = SMACalculator(ma_mid)
        self.sma_long_term  = SMACalculator(ma_long_term)
        self.vol_ratio      = VolumeRatioCalculator(vol_lookback)
        self.atr            = ATRCalculator(atr_period)     # v2.0
        self.rsi            = RSICalculator(14)             # v3.2
        self.macd           = MACDCalculator()

        self.candles:       list[Candle] = []
        self.last_price:    float = 0.0
        self._seen: set[str] = set()      # 이미 반영한 봉 시각 (중복 방지)

        # v2.0: 봉 종가 시점의 VWAP 스냅샷 (확정 이탈 판정용)
        self._vwap_history: list[tuple[float, float]] = []
        # v4.0: 5MA / 20MA 이력 (골든크로스 / 데드크로스 감지용)
        self._ma_history:   list[tuple[float, float]] = []

    # ── 봉 추가 ───────────────────────────────────────────────
    def add_candle(self, candle: Candle) -> bool:
        """새 봉 추가 및 지표 업데이트

        Returns:
            실제로 반영되었으면 True, 중복이라 건너뛰었으면 False
        """
        key = candle.time
        if key in self._seen:
            return False                   # ★ 중복 누적 방지 (VWAP/SMA 오염의 원인)
        self._seen.add(key)

        self.candles.append(candle)
        if len(self.candles) > MAX_CANDLE_HISTORY:
            self.candles.pop(0)

        self.last_price = candle.close
        vwap_now = self.vwap.update(candle)
        self.sma_short.update(candle.close)
        self.sma_long.update(candle.close)
        self.sma_mid.update(candle.close)
        self.sma_long_term.update(candle.close)
        self.vol_ratio.update(candle.volume)
        self.atr.update(candle)                                  # v2.0
        self.rsi.update(candle.close)                            # v3.2
        self.macd.update(candle.close)                           # MACD 추가

        s, l = self.sma_short.value, self.sma_long.value
        if s is not None and l is not None:
            self._ma_history.append((s, l))
            if len(self._ma_history) > MAX_CANDLE_HISTORY:
                self._ma_history.pop(0)

        # v2.0: 확정 봉의 (종가, VWAP) 스냅샷 기록
        self._vwap_history.append((candle.close, float(vwap_now or 0.0)))
        if len(self._vwap_history) > MAX_CANDLE_HISTORY:
            self._vwap_history.pop(0)

        # ★ ORB 판정: 벽시계가 아닌 '봉 자체 시각' 기준
        if self.orb_start <= candle.hhmm < self.orb_end:
            self.orb.update(candle)

        return True

    def add_candles(self, candles: list[Candle]) -> int:
        """봉 리스트 일괄 추가 (시간 오름차순 정렬 후 반영)

        Returns:
            새로 반영된 봉 개수
        """
        ordered = sorted(candles, key=lambda c: c.time)
        return sum(1 for c in ordered if self.add_candle(c))

    def set_orb_ready(self) -> bool:
        """ORB 관찰 구간 종료 선언 — 이후부터 돌파 신호 감지 가능

        Returns:
            ORB가 유효하게 형성되었으면 True
        """
        self.orb.ready = True
        if not self.orb.is_valid:
            logger.warning(
                "[%s] ORB 데이터 없음 — 돌파 신호 비활성 (봉 %d개 수신)",
                self.symbol, len(self.candles),
            )
            return False

        logger.info(
            "[%s] ORB 확정: 고점=%.0f 저점=%.0f 폭=%.2f%% (%d봉)",
            self.symbol, self.orb.high, self.orb.low,
            self.orb.range_pct, self.orb.count,
        )
        return True

    # ── 조건 프로퍼티 ─────────────────────────────────────────
    @property
    def is_above_vwap(self) -> bool:
        return self.vwap.value > 0 and self.last_price > self.vwap.value

    @property
    def is_ma_aligned(self) -> bool:
        """5MA > 20MA 이면서 둘 다 우상향"""
        s, l = self.sma_short.value, self.sma_long.value
        if s is None or l is None:
            return False
        return s > l and self.sma_short.is_rising() and self.sma_long.is_rising()

    @property
    def volume_ratio(self) -> float:
        return self.vol_ratio.current_ratio()

    @property
    def has_enough_data(self) -> bool:
        """지표 판정에 충분한 봉이 쌓였는지"""
        return self.sma_long.value is not None

    @property
    def day_low(self) -> float:
        """당일 최저가 (오늘 수신된 봉들의 최저가)"""
        if not self.candles:
            return 0.0
        return min(c.low for c in self.candles)

    def closed_bars_below_day_low(self, n: int = 1) -> bool:
        """최근 n개 확정 봉의 종가가 당일 저가(day_low) 미만인지 확인"""
        d_low = self.day_low
        if d_low <= 0 or not self.candles or len(self.candles) < n:
            return False
        recent = self.candles[-n:]
        return all(c.close < d_low for c in recent)

    def closed_bars_below_vwap(self, n: int = 1) -> bool:
        """확정된 최근 n개 봉의 종가가 모두 VWAP 아래인지 (v2.0)

        틱 단위 순간 이탈은 휩쏘가 심해 승률을 크게 깎는다.
        '봉 종가가 확정적으로 VWAP 아래' 인 경우에만 이탈로 본다.

        add_candle 은 미확정 봉을 받지 않으므로(parse_candles 의
        exclude_forming=True) 여기 쌓인 이력은 전부 확정 봉이다.
        """
        if n <= 0:
            return False
        if len(self._vwap_history) < n:
            return False
        recent = self._vwap_history[-n:]
        return all(vwap > 0 and close < vwap for close, vwap in recent)

    def is_orb_breakout(self, price: float) -> bool:
        return self.orb.is_breakout(price)

    # ── 눌림목 감지 (v3.2 리팩토링) ───────────────────────────
    def is_support_pullback(self, price: float) -> bool:
        """지지선(VWAP, 5MA, 20MA) 영영 도달 여부

        지지선 위 또는 인근(VWAP 1.5% 이내, 5MA 1.0% 이내, 20MA 1.0% 이내)에
        가격이 도달하였는지 감지.
        """
        vwap = self.vwap.value
        ma5 = self.sma_short.value
        ma20 = self.sma_long.value
        if price <= 0:
            return False

        if vwap > 0 and 0 <= (price - vwap) / vwap * 100 <= 1.5:
            return True
        if ma5 and ma5 > 0 and 0 <= (price - ma5) / ma5 * 100 <= 1.0:
            return True
        if ma20 and ma20 > 0 and 0 <= (price - ma20) / ma20 * 100 <= 1.0:
            return True
        return False

    def is_rebound_candle(self) -> bool:
        """★ 핵심: 반등 확인봉 감지 (떨어지는 칼날 매수 방지)

        마지막 확정 캔들이 양봉(종가 > 시가)이거나,
        아랫꼬리가 길게 형성되어 지지를 확인한 망치형 캔들인 경우에만 참.
        """
        if not self.candles:
            return False
        last_c = self.candles[-1]
        # 조건 1: 확실한 양봉 (종가 > 시가)
        if last_c.close > last_c.open:
            return True
        # 조건 2: 아랫꼬리가 긴 지지 캔들 (종가가 봉 전체 범위의 50% 이상 상단)
        high_low = last_c.high - last_c.low
        if high_low > 0:
            pos_ratio = (last_c.close - last_c.low) / high_low
            if pos_ratio >= 0.5:
                return True
        return False

    @property
    def rsi_value(self) -> Optional[float]:
        """1분봉 RSI 값"""
        return self.rsi.value

    @property
    def atr_pct(self) -> Optional[float]:
        """ATR 비율 (%)"""
        return self.atr.pct

    def is_pullback_to_vwap(self, price: float, threshold_pct: float = 1.0) -> bool:
        """가격이 VWAP 위 0~threshold_pct% 이내로 내려왔는지 (VWAP 지지 터치)

        VWAP 아래로 깨지면 False — 지지가 깨진 것이므로 진입 불가.
        VWAP 바로 위에서 반등을 노리는 것이 핵심.
        """
        vwap = self.vwap.value
        if vwap <= 0 or price <= 0:
            return False
        dist_pct = (price - vwap) / vwap * 100
        return 0 <= dist_pct <= threshold_pct

    def is_pullback_to_ma5(self, price: float, threshold_pct: float = 0.5) -> bool:
        """가격이 5MA 위 0~threshold_pct% 이내로 내려왔는지 (이평선 지지 터치)"""
        ma5 = self.sma_short.value
        if ma5 is None or ma5 <= 0 or price <= 0:
            return False
        dist_pct = (price - ma5) / ma5 * 100
        return 0 <= dist_pct <= threshold_pct

    def disparity_ma20(self, price: float) -> float:
        """20MA 대비 주가 이격도 (%) = (주가 / 20MA) * 100"""
        ma20 = self.sma_long.value
        if ma20 is None or ma20 <= 0 or price <= 0:
            return 100.0
        return (price / ma20) * 100.0

    def is_valid_disparity(self, price: float, min_pct: float = 98.0, max_pct: float = 105.0) -> bool:
        """이격도 범위 검증 (과열 회피 및 적정 눌림목 파악)"""
        disp = self.disparity_ma20(price)
        return min_pct <= disp <= max_pct

    @property
    def is_ma_perfect_aligned(self) -> bool:
        """단기(5MA) > 중기(20MA) > 장기(60MA) 완벽 정배열 & 20MA/5MA 우상향 기울기"""
        ma5, ma20, ma60 = self.sma_short.value, self.sma_long.value, self.sma_mid.value
        if ma5 is None or ma20 is None:
            return False
        aligned = (ma5 > ma20)
        if ma60 is not None and ma60 > 0:
            aligned = aligned and (ma20 > ma60)
        return aligned and self.sma_short.is_rising() and self.sma_long.is_rising()

    @property
    def is_ma_perfect_aligned_4(self) -> bool:
        """단기(5MA) > 중기(20MA) > 장기(60MA) > 장기2(120MA) 4중 정배열"""
        ma5, ma20 = self.sma_short.value, self.sma_long.value
        ma60, ma120 = self.sma_mid.value, self.sma_long_term.value
        if ma5 is None or ma20 is None:
            return False
        if ma5 <= ma20:
            return False
        if ma60 is not None and ma60 > 0 and ma20 <= ma60:
            return False
        if ma120 is not None and ma120 > 0 and ma60 is not None and ma60 > 0 and ma60 <= ma120:
            return False
        return True

    @property
    def are_all_mas_rising(self) -> bool:
        """모든 형성된 이동평균선(5, 20, 60, 120)이 우상향 기울기를 유지하는지"""
        if not (self.sma_short.is_rising() and self.sma_long.is_rising()):
            return False
        if self.sma_mid.value is not None and len(self.sma_mid._history) >= 2:
            if not self.sma_mid.is_rising():
                return False
        if self.sma_long_term.value is not None and len(self.sma_long_term._history) >= 2:
            if not self.sma_long_term.is_rising():
                return False
        return True

    def is_price_above_all_mas(self, price: float) -> bool:
        """주가가 정배열된 이동평균선 위에서 움직이는지 (특히 20MA 상단 안착)"""
        if price <= 0:
            return False
        ma5, ma20 = self.sma_short.value, self.sma_long.value
        ma60, ma120 = self.sma_mid.value, self.sma_long_term.value
        if ma20 is not None and ma20 > 0 and price < ma20:
            return False
        if ma5 is not None and ma5 > 0 and price < ma5 * 0.99:  # 5MA 인근 포함
            return False
        if ma60 is not None and ma60 > 0 and price < ma60:
            return False
        if ma120 is not None and ma120 > 0 and price < ma120:
            return False
        return True

    @property
    def is_golden_cross_5_20(self) -> bool:
        """단기선(5MA)이 장기선(20MA)을 아래에서 위로 돌파 (골든크로스)"""
        if len(self._ma_history) < 2:
            return False
        prev_5, prev_20 = self._ma_history[-2]
        curr_5, curr_20 = self._ma_history[-1]
        return prev_5 <= prev_20 and curr_5 > curr_20

    @property
    def is_dead_cross_5_20(self) -> bool:
        """단기선(5MA)이 장기선(20MA)을 위에서 아래로 돌파 (데드크로스)"""
        if len(self._ma_history) < 2:
            return False
        prev_5, prev_20 = self._ma_history[-2]
        curr_5, curr_20 = self._ma_history[-1]
        return prev_5 >= prev_20 and curr_5 < curr_20

    @property
    def is_ma20_turning_down(self) -> bool:
        """20MA 기울기가 음수(하향 꺾임)로 전환되었는지"""
        return self.sma_long.value is not None and self.sma_long.slope_pct() < 0

    def closed_bars_below_ma20(self, n: int = 1) -> bool:
        """확정된 최근 n개 봉의 종가가 모두 20MA 아래인지 (20MA 이탈 확정)"""
        if n <= 0 or len(self.candles) < n or self.sma_long.value is None:
            return False
        ma20 = self.sma_long.value
        recent = self.candles[-n:]
        return all(c.close < ma20 for c in recent)

    def is_pullback_to_ma20(self, price: float, threshold_pct: float = 1.0) -> bool:
        """가격이 우상향 20MA 위 0~threshold_pct% 이내로 내려왔는지 (20MA 지지 영역)"""
        ma20 = self.sma_long.value
        if ma20 is None or ma20 <= 0 or price <= 0:
            return False
        dist_pct = (price - ma20) / ma20 * 100
        return 0 <= dist_pct <= threshold_pct

    def is_pullback_to_ma60(self, price: float, threshold_pct: float = 1.5) -> bool:
        """가격이 60MA 위/인근 0~threshold_pct% 이내로 내려왔는지 (60MA 주요 지지선 인근)"""
        ma60 = self.sma_mid.value
        if ma60 is None or ma60 <= 0 or price <= 0:
            return False
        dist_pct = (price - ma60) / ma60 * 100
        return 0 <= dist_pct <= threshold_pct

    def closed_bars_below_ma60(self, n: int = 1) -> bool:
        """확정된 최근 n개 봉의 종가가 모두 60MA 아래인지 (60MA 지지선 이탈 확정)"""
        if n <= 0 or len(self.candles) < n or self.sma_mid.value is None:
            return False
        ma60 = self.sma_mid.value
        recent = self.candles[-n:]
        return all(c.close < ma60 for c in recent)

    def is_panic_dump(self, price: float, threshold_vol_ratio: float = 2.0, threshold_dip_pct: float = 3.0) -> bool:
        """당일 투매/패닉 급락 구간인지 (거래량 평소 대비 200% 이상 폭증 + 고점 대비 3% 이상 급락)

        통상 패닉 투매 날 저점에서 손절하면 직후 급반등(휩쏘)이 발생하므로
        손절을 종가 마감 확정 전까지 유보하기 위해 사용한다.
        """
        ratio = self.volume_ratio
        dip = self.dip_from_high_pct(price)
        return ratio >= threshold_vol_ratio and dip >= threshold_dip_pct

    def is_in_uptrend(self) -> bool:
        """전체적인 상승 추세 판정

        조건: 20MA 존재 + 20MA 상승 중(기울기 > 0) + 현재가 > 20MA
        """
        ma20 = self.sma_long.value
        if ma20 is None or ma20 <= 0:
            return False
        return self.last_price > ma20 and self.sma_long.is_rising()

    @property
    def day_high(self) -> float:
        """당일 최고가"""
        if not self.candles:
            return 0.0
        return max(c.high for c in self.candles)

    def dip_from_high_pct(self, price: float) -> float:
        """당일 고점 대비 현재가 하락률 (%)

        양수면 고점보다 아래에 있다는 뜻. 눌림 깊이 측정용.
        """
        high = self.day_high
        if high <= 0 or price <= 0:
            return 0.0
        return (high - price) / high * 100

    def vwap_distance_pct(self, price: float) -> float:
        """VWAP 대비 현재가 거리 (%). 양수=VWAP 위, 음수=VWAP 아래"""
        vwap = self.vwap.value
        if vwap <= 0 or price <= 0:
            return 0.0
        return (price - vwap) / vwap * 100

    def is_volume_decreasing(self) -> bool:
        """최근 봉 거래량이 평균 이하로 감축되었는지 (눌림 시 매도 압력 약화 확인)

        눌림목에서는 거래량이 줄거나 평소 이하이어야 건강한 조정이다.
        거래량이 터지면서 내리면 추세 전환 위험.
        """
        ratio = self.vol_ratio.current_ratio()
        return 0 < ratio <= 1.0

    def summary(self) -> dict:
        return {
            "symbol":     self.symbol,
            "price":      self.last_price,
            "vwap":       round(self.vwap.value, 1),
            "sma5":       self.sma_short.value,
            "sma20":      self.sma_long.value,
            "orb_high":   self.orb.high,
            "orb_valid":  self.orb.is_valid,
            "orb_ready":  self.orb.ready,
            "candles":    len(self.candles),
            "vol_ratio":  round(self.volume_ratio, 2),
            "above_vwap": self.is_above_vwap,
            "ma_aligned": self.is_ma_aligned,
        }


# ──────────────────────────────────────────────
#  KIS API 응답 → Candle 변환 헬퍼
# ──────────────────────────────────────────────
def parse_candle_from_api(row: dict) -> Optional[Candle]:
    """KIS 분봉 API 응답 한 행 → Candle 객체"""
    try:
        c = Candle(
            time   = str(row.get("stck_cntg_hour", "000000")),
            open   = float(row.get("stck_oprc", 0)),
            high   = float(row.get("stck_hgpr", 0)),
            low    = float(row.get("stck_lwpr", 0)),
            close  = float(row.get("stck_prpr", 0)),
            volume = int(row.get("cntg_vol", 0)),
        )
    except (ValueError, TypeError, KeyError) as e:
        logger.warning("Candle 파싱 실패: %s | 원본: %s", e, row)
        return None

    # 무결성 검사 — 비정상 봉 제외
    if c.high <= 0 or c.low <= 0 or c.close <= 0:
        return None
    if c.high < c.low:
        logger.warning("비정상 봉 (high<low): %s", c)
        return None
    return c


def resample(candles: list[Candle], minutes: int) -> list[Candle]:
    """1분봉 → N분봉 집계

    KIS 분봉 API(FHKST03010200)는 1분봉을 반환한다.
    이를 N분봉으로 쓰려면 반드시 집계해야 하며, 집계 없이 그대로 사용하면
    이동평균·거래량배율의 실제 기간이 설정값과 달라진다.
    """
    if minutes <= 1 or not candles:
        return sorted(candles, key=lambda c: c.time)

    buckets: dict[str, list[Candle]] = {}
    for c in sorted(candles, key=lambda c: c.time):
        hh, mm = int(c.hhmm[:2]), int(c.hhmm[2:])
        key = f"{hh:02d}{(mm // minutes) * minutes:02d}"
        buckets.setdefault(key, []).append(c)

    out: list[Candle] = []
    for key in sorted(buckets):
        grp = buckets[key]
        out.append(Candle(
            time   = key + "00",
            open   = grp[0].open,
            high   = max(c.high for c in grp),
            low    = min(c.low  for c in grp),
            close  = grp[-1].close,
            volume = sum(c.volume for c in grp),
        ))
    return out


def warmup_minutes(ma_long: int, bar_minutes: int) -> int:
    """지표 성숙에 필요한 시간(분)"""
    return ma_long * bar_minutes


def parse_candles(rows: list[dict], exclude_forming: bool = True,
                  now_hhmm: Optional[str] = None, bar_minutes: int = 5) -> list[Candle]:
    """KIS 분봉 응답 전체 → Candle 리스트

    Args:
        exclude_forming: 아직 진행 중인(미완성) 마지막 봉을 제외할지
        now_hhmm: 현재 시각 'HHMM' (미지정 시 제외 로직 생략)
        bar_minutes: 봉 주기
    """
    candles = [c for c in (parse_candle_from_api(r) for r in rows) if c and c.volume > 0]
    candles.sort(key=lambda c: c.time)

    # API는 1분봉을 반환하므로 설정 주기로 집계
    if bar_minutes > 1:
        candles = resample(candles, bar_minutes)

    if exclude_forming and now_hhmm and candles:
        # 현재 진행 중인(미완성) 봉의 시작 시각 계산 후 제외
        hh, mm = int(now_hhmm[:2]), int(now_hhmm[2:])
        bucket_min = (mm // bar_minutes) * bar_minutes
        forming = f"{hh:02d}{bucket_min:02d}"
        candles = [c for c in candles if c.hhmm < forming]

    return candles
