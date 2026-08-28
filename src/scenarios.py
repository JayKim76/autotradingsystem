"""
scenarios.py — 시장 국면별 백테스트 시나리오

과거 실데이터가 충분히 쌓이기 전에도 전략의 강건성을 검증할 수 있도록,
스켈핑에서 실제로 마주치는 국면들을 합성 분봉으로 재현한다.

각 시나리오는 데이터뿐 아니라 **기대 동작**을 함께 선언한다.
따라서 "수익이 났는가"가 아니라 "설계대로 반응했는가"를 검증한다.
이것이 합성 데이터로 할 수 있는 유일하게 의미 있는 검증이다.

사용::

    from src.scenarios import ALL_SCENARIOS, build
    sc = build("bull_trap")
    print(sc.description, len(sc.candles))
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Optional

from .indicators import Candle

# 정규장 1분봉 개수: 09:00 ~ 15:29 = 390분
BARS = 390
OPEN_HOUR = 9


# ──────────────────────────────────────────────
#  시나리오 정의
# ──────────────────────────────────────────────
@dataclass
class Scenario:
    """검증 시나리오 — 데이터 + 기대 동작"""
    name:         str
    title:        str
    description:  str
    candles:      list[Candle]
    gap_pct:      float
    expect_entry: bool                       # 진입이 발생해야 하는가
    expect_exits: list[str] = field(default_factory=list)   # 기대 청산 사유 키워드
    expect_pnl:   str = "any"                # positive / negative / flat / any
    rationale:    str = ""                   # 왜 그렇게 기대하는가
    max_loss_pct: Optional[float] = None     # 허용 최대 손실률 (자본 대비)

    @property
    def bar_count(self) -> int:
        return len(self.candles)


# ──────────────────────────────────────────────
#  분봉 생성 엔진
# ──────────────────────────────────────────────
def _minute_to_time(i: int) -> str:
    """봉 인덱스 → 'HHMMSS' (09:00 기준). 점심시간 없는 연속장."""
    total = OPEN_HOUR * 60 + i
    return f"{total // 60:02d}{total % 60:02d}00"


def _build_day(
    segments: list[tuple[int, int, float, float, float]],
    *,
    prev_close: float = 10_000.0,
    gap_pct: float = 0.0,
    base_volume: int = 500,
    seed: int = 0,
) -> list[Candle]:
    """구간별 드리프트로 하루치 1분봉 생성

    Args:
        segments: (시작분, 종료분, 분당드리프트, 거래량배율, 노이즈) 리스트
        prev_close: 전일 종가
        gap_pct: 시가 갭 (%)
        base_volume: 기준 분당 거래량
    """
    rng = random.Random(seed)
    price = prev_close * (1 + gap_pct / 100)

    # 구간 조회용 확장
    plan: list[tuple[float, float, float]] = [(0.0, 1.0, 0.0015)] * BARS
    for start, end, drift, volmul, noise in segments:
        for i in range(max(0, start), min(BARS, end)):
            plan[i] = (drift, volmul, noise)

    candles: list[Candle] = []
    for i in range(BARS):
        drift, volmul, noise = plan[i]
        o = price
        price *= (1 + drift + rng.gauss(0, noise))
        c = price

        span = abs(c - o) + o * abs(rng.gauss(0, noise)) * 0.6
        h = max(o, c) + span * rng.uniform(0.1, 0.5)
        l = min(o, c) - span * rng.uniform(0.1, 0.5)

        vol = int(base_volume * volmul * rng.uniform(0.6, 1.5))

        candles.append(Candle(
            time=_minute_to_time(i),
            open=round(o), high=round(h),
            low=round(max(1.0, l)), close=round(c),
            volume=max(1, vol),
        ))

    return candles


# ──────────────────────────────────────────────
#  개별 시나리오 빌더
# ──────────────────────────────────────────────
def _trend_up() -> Scenario:
    """정상 추세일 — 전략이 가장 잘 작동해야 하는 국면"""
    candles = _build_day(
        [
            (0,   30, 0.00030, 1.0, 0.0012),   # ORB 구간: 완만한 등락
            (30,  50, 0.00220, 5.0, 0.0015),   # 돌파 + 거래량 폭증
            (50, 130, 0.00110, 2.5, 0.0013),   # 추세 지속
            (130, 390, 0.00005, 1.0, 0.0011),  # 오후 소강
        ],
        gap_pct=4.0, base_volume=2200, seed=11,
    )
    return Scenario(
        name="trend_up",
        title="갭상승 추세일",
        description="갭 +4% 후 ORB를 거래량과 함께 돌파하고 오전 내내 상승",
        candles=candles, gap_pct=4.0,
        expect_entry=True,
        expect_exits=["TP1", "TP2"],
        expect_pnl="positive",
        rationale=(
            "5개 조건이 모두 충족되는 교과서적 국면. 진입 후 익절 사다리가 "
            "TP1→TP2 순서로 작동하고 트레일링으로 잔량을 관리해야 한다."
        ),
    )


def _bull_trap() -> Scenario:
    """함정일 — 스켈핑 최대 손실 원인"""
    candles = _build_day(
        [
            (0,   30,  0.00035, 1.0, 0.0012),  # ORB 형성
            (30,  38,  0.00300, 6.0, 0.0016),  # 돌파 (매수 유인)
            (38,  75, -0.00330, 4.0, 0.0022),  # 즉시 급반락 ★함정
            (75,  390, -0.00020, 1.2, 0.0014),
        ],
        gap_pct=5.0, base_volume=2400, seed=23,
    )
    return Scenario(
        name="bull_trap",
        title="갭상승 함정일 (Bull Trap)",
        description="갭 +5%, ORB 돌파 직후 급반락 — 돌파 매수자를 물리는 전형",
        candles=candles, gap_pct=5.0,
        expect_entry=True,
        expect_exits=["손절", "VWAP"],
        expect_pnl="negative",
        max_loss_pct=2.0,
        rationale=(
            "진입 자체는 정당하다(조건 충족). 핵심은 **손실을 얼마나 빨리 끊는가**. "
            "손절 -1% 또는 VWAP 이탈이 즉시 작동해 자본 대비 2% 이내로 막아야 한다. "
            "이 시나리오에서 손실이 크면 손절 로직에 문제가 있다."
        ),
    )


def _choppy() -> Scenario:
    """횡보일 — 신호는 뜨는데 추세가 없음"""
    segs = [(0, 30, 0.0002, 1.0, 0.0012)]
    for k in range(30, 390, 20):             # 20분 주기 상하 진동
        drift = 0.0011 if (k // 20) % 2 == 0 else -0.0011
        segs.append((k, k + 20, drift, 1.8, 0.0018))
    candles = _build_day(segs, gap_pct=3.5, base_volume=2000, seed=37)
    return Scenario(
        name="choppy",
        title="횡보 톱니일",
        description="갭 +3.5%지만 방향성 없이 20분 주기로 진동",
        candles=candles, gap_pct=3.5,
        expect_entry=True,
        expect_exits=["손절", "VWAP", "본절"],
        expect_pnl="any",
        max_loss_pct=3.0,
        rationale=(
            "가짜 돌파가 반복되는 국면. 재진입 쿨다운과 종목당 거래 한도가 "
            "작동해 손실 누적을 막아야 한다. 거래 횟수가 한도를 넘으면 안 된다."
        ),
    )


def _midday_crash() -> Scenario:
    """장중 급락 — 확보한 이익을 지킬 수 있는가"""
    candles = _build_day(
        [
            (0,   30,  0.00030, 1.0, 0.0012),
            (30,  60,  0.00250, 5.0, 0.0015),   # 돌파 상승
            (60,  110, 0.00090, 2.0, 0.0013),   # 추가 상승 (TP2 도달)
            (110, 150, -0.00400, 6.0, 0.0028),  # ★장중 급락
            (150, 390, -0.00030, 1.5, 0.0016),
        ],
        gap_pct=4.5, base_volume=2300, seed=51,
    )
    return Scenario(
        name="midday_crash",
        title="장중 급락 반전일",
        description="오전 +6% 상승 후 11시경 급락 반전",
        candles=candles, gap_pct=4.5,
        expect_entry=True,
        expect_exits=["TP1", "TP2", "트레일링", "5MA"],
        expect_pnl="positive",
        rationale=(
            "분할 익절의 존재 이유를 검증하는 시나리오. 급락 전에 TP1·TP2로 "
            "80%를 확보하고, 잔량은 트레일링/5MA 이탈로 정리해 **최종 수익**으로 "
            "마감해야 한다. 손실로 끝나면 익절 사다리가 제 역할을 못 한 것이다."
        ),
    )


def _gap_down() -> Scenario:
    """갭하락일 — 진입 자체가 없어야 함"""
    candles = _build_day(
        [
            (0,   30,  0.00020, 1.2, 0.0014),
            (30,  120, 0.00150, 2.5, 0.0016),   # 반등하지만
            (120, 390, 0.00020, 1.0, 0.0012),
        ],
        gap_pct=-3.0, base_volume=600, seed=67,
    )
    return Scenario(
        name="gap_down",
        title="갭하락 반등일",
        description="갭 -3% 하락 출발 후 장중 반등",
        candles=candles, gap_pct=-3.0,
        expect_entry=False,
        expect_pnl="flat",
        rationale=(
            "반등해서 수익 기회처럼 보여도, 전략은 **갭상승 종목만** 다룬다. "
            "스크리너 갭 필터(min_gap_pct=3.0)에서 걸러져 진입이 없어야 한다. "
            "진입이 발생하면 필터가 새는 것이다."
        ),
    )


def _low_volume() -> Scenario:
    """저유동성일 — 거래량 확증 실패"""
    candles = _build_day(
        [
            (0,   30, 0.00030, 1.0, 0.0010),
            (30,  60, 0.00200, 1.1, 0.0012),   # 상승하지만 거래량 증가 없음 ★
            (60, 390, 0.00040, 1.0, 0.0010),
        ],
        gap_pct=4.0, base_volume=90, seed=79,   # 절대 거래량도 빈약
    )
    return Scenario(
        name="low_volume",
        title="저유동성 상승일",
        description="갭 +4%에 가격은 오르지만 거래량 동반 없음",
        candles=candles, gap_pct=4.0,
        expect_entry=False,
        expect_pnl="flat",
        rationale=(
            "거래량 없는 상승은 신뢰할 수 없다. 4번 항목(거래량 배율 1.5배)이 "
            "미충족되어 스코어가 3점 미만이어야 한다. 유동성 부족은 스켈핑에서 "
            "청산 불가로 직결되므로 진입하면 안 된다."
        ),
    )


def _vwap_whipsaw() -> Scenario:
    """VWAP 진동 — 손절 과다 발동 확인"""
    segs = [(0, 30, 0.0002, 1.0, 0.0012)]
    for k in range(30, 390, 12):             # 12분 주기 빠른 진동
        drift = 0.0018 if (k // 12) % 2 == 0 else -0.0019
        segs.append((k, k + 12, drift, 2.2, 0.0024))
    candles = _build_day(segs, gap_pct=4.0, base_volume=2100, seed=91)
    return Scenario(
        name="vwap_whipsaw",
        title="VWAP 휩소일",
        description="VWAP 위아래를 12분 주기로 반복 교차",
        candles=candles, gap_pct=4.0,
        expect_entry=True,
        expect_exits=["VWAP", "손절"],
        expect_pnl="any",
        max_loss_pct=3.0,
        rationale=(
            "VWAP 손절이 잦게 발동하는 최악의 국면. 일일 손실 한도(-3%)와 "
            "연속 손실 차단(3회)이 작동해 손실이 한도 내에서 멈춰야 한다. "
            "한도를 넘으면 안전장치가 작동하지 않은 것이다."
        ),
    )


def _slow_grind() -> Scenario:
    """완만한 상승 — TP1만 도달하고 끝"""
    candles = _build_day(
        [
            (0,   30, 0.00025, 1.0, 0.0011),
            (30,  70, 0.00075, 3.0, 0.0012),   # TP1(+1.5%)만 겨우 도달
            (70, 390, 0.00003, 1.2, 0.0010),   # 이후 정체
        ],
        gap_pct=3.2, base_volume=2000, seed=103,
    )
    return Scenario(
        name="slow_grind",
        title="완만 상승 정체일",
        description="갭 +3.2%, TP1 도달 후 상승 멈추고 장 마감까지 횡보",
        candles=candles, gap_pct=3.2,
        expect_entry=True,
        expect_exits=["TP1", "강제청산", "본절"],
        expect_pnl="any",
        rationale=(
            "가장 흔한 국면. TP1으로 50%를 확보한 뒤 잔량은 본절 스톱 또는 "
            "15:15 강제청산으로 정리되어야 한다. 오버나잇 보유가 남으면 "
            "강제청산 로직에 문제가 있다."
        ),
    )


def _limit_up() -> Scenario:
    """상한가 근접 — 스크리너가 걸러야 함"""
    candles = _build_day(
        [
            (0,   30, 0.00250, 8.0, 0.0025),
            (30,  90, 0.00350, 10.0, 0.0030),  # 상한가 직행
            (90, 390, 0.00002, 3.0, 0.0008),   # 상한가 잠김 (거래 정체)
        ],
        gap_pct=9.0, base_volume=900, seed=117,
    )
    return Scenario(
        name="limit_up",
        title="상한가 직행일",
        description="갭 +9% 후 상한가 근접까지 급등, 이후 거래 정체",
        candles=candles, gap_pct=9.0,
        expect_entry=False,
        expect_pnl="flat",
        rationale=(
            "수익이 커 보이지만 상한가는 **매도 체결이 불가능**해질 위험이 있다. "
            "스크리너의 max_change_pct(25%)와 max_gap_pct(15%) 필터, "
            "그리고 추격 매수 차단(ORB 고점 +2%)이 진입을 막아야 한다."
        ),
    )


# ──────────────────────────────────────────────
#  레지스트리
# ──────────────────────────────────────────────
_BUILDERS: dict[str, Callable[[], Scenario]] = {
    "trend_up":      _trend_up,
    "bull_trap":     _bull_trap,
    "midday_crash":  _midday_crash,
    "choppy":        _choppy,
    "vwap_whipsaw":  _vwap_whipsaw,
    "slow_grind":    _slow_grind,
    "gap_down":      _gap_down,
    "low_volume":    _low_volume,
    "limit_up":      _limit_up,
}

ALL_SCENARIOS: list[str] = list(_BUILDERS.keys())

# 진입이 발생해야 하는 시나리오 / 걸러져야 하는 시나리오
ENTRY_EXPECTED  = ["trend_up", "bull_trap", "midday_crash",
                   "choppy", "vwap_whipsaw", "slow_grind"]
FILTER_EXPECTED = ["gap_down", "low_volume", "limit_up"]


def build(name: str) -> Scenario:
    """이름으로 시나리오 생성"""
    if name not in _BUILDERS:
        raise KeyError(f"알 수 없는 시나리오: {name} (가능: {', '.join(ALL_SCENARIOS)})")
    return _BUILDERS[name]()


def build_all() -> list[Scenario]:
    return [build(n) for n in ALL_SCENARIOS]


def export_csv(scenario: Scenario, path: str) -> None:
    """시나리오를 CSV로 저장 (외부 도구 분석용)"""
    import csv as _csv
    from pathlib import Path as _Path

    p = _Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for c in scenario.candles:
            w.writerow([c.time, int(c.open), int(c.high),
                        int(c.low), int(c.close), c.volume])
