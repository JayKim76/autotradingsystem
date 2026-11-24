"""
live_guard.py — Phase 0 실전 잠금 (2026-11-24, 고도화 기획안 Phase 0)

배경: 지금까지 검증한 7개 전략이 전부 사전 고정 게이트에서 FAIL했다.
엣지가 확인되지 않은 전략으로 실전(real) 주문을 내는 것은 기대값이
음수인 도박이므로, 실전 경로는 코드 레벨에서 잠근다.

잠금 규칙:
  - TRADE_MODE=paper (모의) / dry-run 은 항상 허용.
  - TRADE_MODE=real 은 환경변수 LIVE_TRADING_ENABLED 가
    1/true/yes 중 하나일 때만 허용. 그 외에는 RuntimeError로 즉시 중단.

해제 조건 (전부 충족 + 사용자 명시 결정 후 .env에만 설정 — 코드 수정 금지):
  1. 신규 전략이 미사용 데이터 또는 paper 전진 검증에서 게이트 PASS
  2. paper 전진 검증 30건 이상 누적
  3. 사전등록 문서(사전등록양식.md)가 작성·커밋되어 있을 것
  4. 사용자가 해제 사실을 알고 명시적으로 결정했을 것

해제는 "전략 1개"에 대해서만 유효한 운영 절차이며, 이 모듈의 존재
자체가 해제 기록을 대체하지 않는다. 해제·재잠금 이력은
데이터사용대장.md 의 운영 기록에 남긴다.
"""
from __future__ import annotations

import os

UNLOCK_ENV = "LIVE_TRADING_ENABLED"
_TRUTHY = ("1", "true", "yes")


def live_trading_enabled() -> bool:
    """실전 잠금이 해제돼 있는지 (환경변수 기준)"""
    return os.getenv(UNLOCK_ENV, "").strip().lower() in _TRUTHY


def assert_live_trading_allowed(mode: str) -> None:
    """mode가 real인데 잠금이 걸려 있으면 RuntimeError.

    paper 등 그 외 모드는 아무것도 하지 않는다.
    """
    if (mode or "").strip().lower() != "real":
        return
    if live_trading_enabled():
        return
    raise RuntimeError(
        "실전(real) 모드가 Phase 0 잠금으로 차단됐습니다. "
        "검증 게이트를 통과한 전략이 아직 없고 paper 전진 검증 30건도 "
        "쌓이지 않았습니다. 모의로 실행하려면 TRADE_MODE=paper로 두세요. "
        f"해제는 해제 조건 4가지를 모두 충족한 뒤에만 {UNLOCK_ENV}=1 로 "
        "가능합니다 (live_guard.py 상단 문서 참조)."
    )
