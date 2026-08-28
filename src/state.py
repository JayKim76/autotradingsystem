"""
state.py — 크래시 복구용 상태 스냅샷

프로세스가 장중에 죽으면 메모리상 포지션 정보가 사라지는 반면
브로커에는 실제 포지션이 그대로 남는다. 이 불일치는 손절 미작동으로
이어지므로, 매 틱마다 상태를 디스크에 저장하고 재시작 시 복원한다.

복원 시에는 반드시 브로커 실제 잔고와 대사(reconcile)해야 한다.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .risk_manager import RiskManager, Position, DailyStats

KST = ZoneInfo("Asia/Seoul")
logger = logging.getLogger("autotrader.state")


class StateStore:
    """리스크 매니저 상태를 JSON으로 저장/복원"""

    def __init__(self, path: str = "data/state.json") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── 저장 ──────────────────────────────────────────────────
    def save(self, risk: RiskManager) -> None:
        """원자적 저장 (임시 파일 → rename)"""
        payload = {
            "saved_at":  datetime.now(tz=KST).isoformat(),
            "date":      risk.daily.date,
            "capital":   risk.capital,
            "day_start_capital":   risk.day_start_capital,
            "month_start_capital": risk.month_start_capital,
            "month_pnl": risk.month_pnl,
            "daily":     asdict(risk.daily),
            "positions": {s: asdict(p) for s, p in risk.positions.items()},
            "symbol_trades": risk._symbol_trades,
            "cooldown": {
                s: t.isoformat() for s, t in risk._cooldown.items()
            },
        }

        try:
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.error("상태 저장 실패: %s", e)

    # ── 복원 ──────────────────────────────────────────────────
    def load(self) -> Optional[dict]:
        if not self.path.exists():
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("상태 로드 실패: %s", e)
            return None

    def restore(self, risk: RiskManager) -> bool:
        """저장된 상태를 RiskManager에 복원

        당일 데이터가 아니면 복원하지 않는다 (전일 포지션 오염 방지).

        Returns:
            복원 성공 여부
        """
        data = self.load()
        if not data:
            return False

        today = datetime.now(tz=KST).strftime("%Y%m%d")
        if data.get("date") != today:
            logger.info("저장된 상태가 당일(%s)이 아님 — 복원 생략", today)
            return False

        try:
            live_capital = risk.capital
            saved_capital = float(data.get("capital", live_capital))
            diff = live_capital - saved_capital

            risk.capital = live_capital
            risk.day_start_capital    = float(data.get("day_start_capital", live_capital)) + diff
            risk.month_start_capital  = float(data.get("month_start_capital", live_capital)) + diff

            if abs(diff) >= 1000:
                logger.info("입출금/자본 변동 감지: %+f원 (저장값: %.0f -> 현재값: %.0f)", diff, saved_capital, live_capital)
            risk.month_pnl            = float(data.get("month_pnl", 0.0))

            d = data.get("daily", {})
            risk.daily = DailyStats(**{
                k: v for k, v in d.items() if k in DailyStats.__annotations__
            })

            risk.positions = {}
            for sym, pd in data.get("positions", {}).items():
                fields = {k: v for k, v in pd.items() if k in Position.__annotations__}
                risk.positions[sym] = Position(**fields)

            risk._symbol_trades = dict(data.get("symbol_trades", {}))
            risk._cooldown = {
                s: datetime.fromisoformat(t)
                for s, t in data.get("cooldown", {}).items()
            }

            logger.info(
                "상태 복원 완료: 포지션 %d개, 당일 %d거래, 순손익 %.0f원",
                len(risk.positions), risk.daily.trades, risk.daily.pnl,
            )
            return True

        except Exception as e:
            logger.error("상태 복원 실패: %s", e)
            return False

    def clear(self) -> None:
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception as e:
            logger.warning("상태 파일 삭제 실패: %s", e)


# ──────────────────────────────────────────────
#  브로커 잔고 대사
# ──────────────────────────────────────────────
def reconcile_positions(risk: RiskManager, holdings: list[dict], symbol_names: Optional[dict[str, str]] = None) -> list[str]:
    """내부 포지션 장부와 브로커 실제 보유 종목을 대사

    Args:
        holdings: KISClient.get_holdings() 결과
        symbol_names: 종목코드-종목명 매핑 딕셔너리 (전달 시 잔고 종목명 등록)

    Returns:
        경고 메시지 목록
    """
    warnings: list[str] = []

    def _label(sym: str) -> str:
        if symbol_names:
            name = symbol_names.get(sym, "")
            if name and name != sym:
                return f"{sym}({name})"
        return sym

    broker: dict[str, tuple[int, float]] = {}
    for h in holdings:
        sym = str(h.get("pdno", ""))
        name = str(h.get("prdt_name", "") or h.get("hts_kor_isnm", "")).strip()
        qty = int(h.get("hldg_qty", 0) or 0)
        avg = float(h.get("pchs_avg_pric", 0) or 0)
        if sym and qty > 0:
            broker[sym] = (qty, avg)
            if name and symbol_names is not None:
                symbol_names[sym] = name

    internal = set(risk.positions.keys())
    external = set(broker.keys())

    # 1. 브로커에는 있는데 장부에 없음 → 고아 포지션 (크래시 흔적)
    for sym in external - internal:
        qty, avg = broker[sym]
        label = _label(sym)
        msg = f"고아 포지션 발견: {label} {qty}주 @{avg:,.0f}원 — 장부에 편입"
        logger.warning(msg)
        warnings.append(msg)
        risk.positions[sym] = Position(
            symbol      = sym,
            entry_price = avg,
            qty         = qty,
            entry_time  = datetime.now(tz=KST).strftime("%H:%M:%S"),
            score       = 0,
            initial_qty = qty,
            peak_price  = avg,
        )

    # 2. 장부에는 있는데 브로커에 없음 → 이미 청산됨
    for sym in internal - external:
        label = _label(sym)
        msg = f"유령 포지션 제거: {label} — 브로커 잔고에 없음"
        logger.warning(msg)
        warnings.append(msg)
        del risk.positions[sym]

    # 3. 수량 불일치 → 브로커 기준으로 보정
    for sym in internal & external:
        qty, avg = broker[sym]
        pos = risk.positions[sym]
        if pos.qty != qty:
            label = _label(sym)
            msg = f"수량 불일치 보정: {label} 장부 {pos.qty}주 → 실제 {qty}주"
            logger.warning(msg)
            warnings.append(msg)
            pos.qty = qty
            if pos.initial_qty < qty:
                pos.initial_qty = qty

    if not warnings:
        logger.info("잔고 대사 완료 — 불일치 없음 (포지션 %d개)", len(risk.positions))

    return warnings
