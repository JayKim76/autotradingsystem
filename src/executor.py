"""
executor.py — 주문 실행 + 분할 익절

v2.1 수정사항
  - [신규] scale_in() — 추가매수(피라미딩/물타기) 주문 실행
    · 매수 주문 → 체결 → 평균단가 갱신 → 손절선 재계산 → 일지 기록 → 알림

v1.1 수정사항
  - [치명] 강제청산(15:15)이 지정가라 미체결 위험 → 시장가 전환
  - [치명] 주문 실패 시 무조건 재시도하던 로직 → 착지 검증 후 조건부 재시도 (이중체결 방지)
  - 실제 체결가 조회 후 반영 (지정가 가정 → 실체결 기준 손익)
  - 매수 체결도 매매일지에 기록 (기존에는 매도만 기록)
  - 매수 수수료를 진입 시점에 반영
  - ARM_TP3 신호 처리 (청산 없이 트레일링 무장)
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .kis_client import KISClient, KISAPIError
from .risk_manager import RiskManager, Position
from .strategy import StrategyEngine, SignalType, ScaleInSignal
from .indicators import SymbolIndicators
from .journal import TradeJournal
from .notifier import Notifier
from .utils import round_to_tick, now_kst

logger = logging.getLogger("autotrader.executor")

_REASON_LABEL = {
    SignalType.EXIT_TP1:   "TP1",
    SignalType.EXIT_TP2:   "TP2",
    SignalType.EXIT_TP3:   "TP3",
    SignalType.EXIT_SL:    "손절",
    SignalType.EXIT_FORCE: "강제청산",
}


class Executor:
    """주문 실행 오케스트레이터"""

    def __init__(
        self,
        client: KISClient,
        risk: RiskManager,
        strategy: StrategyEngine,
        journal: TradeJournal,
        notifier: Notifier,
        config: dict,
        dry_run: bool = False,
        ai_agent=None,
    ) -> None:
        self.client   = client
        self.risk     = risk
        self.strategy = strategy
        self.journal  = journal
        self.notifier = notifier
        self.dry_run  = dry_run
        self.ai_agent = ai_agent  # v3.1: 매도 시 시장 스냅샷 기록용

        # 종목코드 → 종목명 매핑 (텔레그램 알림용)
        self.symbol_names: dict[str, str] = {}

        exec_cfg = config.get("execution", {})
        self.order_type    = str(exec_cfg.get("order_type", "00"))
        self.slippage_bps  = int(exec_cfg.get("slippage_bps", 5))
        self.fill_wait_sec = float(exec_cfg.get("fill_wait_sec", 2.0))
        self.confirm_fill  = bool(exec_cfg.get("confirm_fill", True))

        # v2.0: 손절은 시장가로. 지정가 손절은 급락 시 미체결로
        #       손실이 무한정 확대된다. "01" 이면 시장가 강제.
        self.stop_order_type = str(exec_cfg.get("stop_order_type", "") or "")

        cost_cfg = config.get("cost", {})
        self.commission_pct = float(cost_cfg.get("commission_pct", 0.015))
        self.tax_pct        = float(cost_cfg.get("tax_pct", 0.20))   # v2.0: 2026년 인상

    # ── 종목 표시명 ────────────────────────────────────────────
    def register_symbol_name(self, symbol: str, name: str) -> None:
        """종목코드 및 종목명 매핑 등록"""
        if symbol and name and name != symbol:
            self.symbol_names[symbol] = name
            if hasattr(self.notifier, "register_symbol"):
                self.notifier.register_symbol(symbol, name)

    def _label(self, symbol: str) -> str:
        """종목코드(종목이름) 형식 반환. 미등록 종목 시 KIS API로 조회 시도."""
        name = self.symbol_names.get(symbol, "")
        if not name and hasattr(self.notifier, "symbol_names"):
            name = self.notifier.symbol_names.get(symbol, "")

        if not name and self.client:
            try:
                fetched_name = self.client.get_symbol_name(symbol)
                if fetched_name and fetched_name != symbol:
                    name = fetched_name
                    self.register_symbol_name(symbol, name)
            except Exception:
                pass

        if name and name != symbol:
            return f"{symbol}({name})"
        return symbol

    # ── 비용 계산 ─────────────────────────────────────────────
    def _buy_cost(self, price: float, qty: int) -> float:
        """매수 비용 (수수료만, 거래세 없음)"""
        return price * qty * self.commission_pct / 100

    def _sell_cost(self, price: float, qty: int) -> float:
        """매도 비용 (수수료 + 거래세)"""
        amount = price * qty
        return amount * (self.commission_pct + self.tax_pct) / 100

    # ── 매수 진입 ─────────────────────────────────────────────
    def enter(self, symbol: str, price: int, score: int = 0,
              available_cash: Optional[float] = None,
              indicators: Optional[SymbolIndicators] = None) -> Optional[Position]:
        """매수 주문 실행 후 Position 반환

        v2.0: indicators 를 주면 진입 시점 ATR로 손절폭(stop_pct)과
              1R 크기(r_unit_pct)를 확정해 Position 에 고정 저장한다.
              이후 매 틱 재계산하지 않으므로 손절선이 흔들리지 않는다.
        """
        if not self.risk.can_enter(symbol):
            return None

        # ── 포지션별 리스크 단위 확정 (v2.0) ──────────────────
        atr_pct = indicators.atr_pct if indicators is not None else None
        stop_pct = self.strategy.resolve_stop_pct(atr_pct)
        r_unit_pct = self.strategy.resolve_r_unit_pct(stop_pct)

        if self.strategy.sl_mode == "atr":
            logger.info(
                "[%s] 손절폭 확정: ATR=%s → %.2f%% (1R=%.2f%%)",
                symbol, f"{atr_pct:.2f}%" if atr_pct else "미성숙(폴백)",
                stop_pct, r_unit_pct,
            )

        qty = self.risk.calc_position_size(price, available_cash, stop_pct=stop_pct)
        if qty <= 0:
            logger.info("[%s] 주문 수량 0 — 진입 취소", symbol)
            return None

        order_price = self._buy_price(price)

        logger.info(
            "[%s] 매수 진입 %d주 @%d원 (스코어 %d/5)%s",
            symbol, qty, order_price, score, " [DRY-RUN]" if self.dry_run else "",
        )

        fill_price = float(order_price)
        odno = ""

        if not self.dry_run:
            odno = self._send_order(symbol, "BUY", qty, order_price)
            if not odno:
                logger.error("[%s] 매수 주문 실패 — 진입 취소", symbol)
                return None
            fill_price, filled_qty = self._resolve_fill(odno, order_price, qty, symbol=symbol)
            if filled_qty <= 0:
                logger.warning("[%s] 매수 미체결 — 주문 취소 후 진입 중단 (odno=%s)", symbol, odno)
                return None
            qty = filled_qty

        cost = self._buy_cost(fill_price, qty)

        pos = Position(
            symbol      = symbol,
            entry_price = fill_price,
            qty         = qty,
            entry_time  = now_kst().strftime("%H:%M:%S"),
            score       = score,
            stop_pct    = stop_pct,      # v2.0
            r_unit_pct  = r_unit_pct,    # v2.0
        )
        self.risk.open_position(pos, cost=cost)

        # 매수도 일지에 기록 (기존 버전 누락)
        self.journal.record(
            symbol      = symbol,
            side        = "BUY",
            qty         = qty,
            price       = fill_price,
            entry_price = fill_price,
            exit_reason = "",
            pnl         = 0.0,
            fee         = cost,
            tax         = 0.0,
            slippage    = abs(fill_price - price) * qty,
            score       = score,
            note        = f"odno={odno}" if odno else "DRY-RUN",
        )

        self.notifier.send(
            f"📈 매수 진입\n종목: {self._label(symbol)}\n{qty}주 @{fill_price:,.0f}원\n스코어 {score}/5"
        )
        return pos

    # ── 매도 청산 ─────────────────────────────────────────────
    def exit(
        self,
        pos: Position,
        current_price: int,
        signal: SignalType,
        reason: str = "",
        market_order: bool = False,
        qty_override: Optional[int] = None,
    ) -> float:
        """매도 주문 실행 후 순손익 반환

        v2.0: qty_override 로 분할 강제청산 수량을 직접 지정할 수 있다.
        """
        if qty_override is not None:
            qty = max(0, min(int(qty_override), pos.qty))
        else:
            qty = self.strategy.exit_qty(pos.qty, pos.initial_qty, signal)
        if qty <= 0:
            return 0.0

        # v2.0: 손실 회피성 청산은 전부 시장가. 익절은 지정가 유지.
        use_market = (
            market_order
            or signal == SignalType.EXIT_FORCE
            or (self.stop_order_type == "01" and signal == SignalType.EXIT_SL)
        )
        order_price = current_price if use_market else self._sell_price(current_price)
        label = _REASON_LABEL.get(signal, signal.name)

        logger.info(
            "[%s] 매도 [%s] %d주 @%s%s",
            pos.symbol, label, qty,
            "시장가" if use_market else f"{order_price}원",
            " [DRY-RUN]" if self.dry_run else "",
        )

        fill_price = float(order_price)
        odno = ""

        if not self.dry_run:
            odno = self._send_order(
                pos.symbol, "SELL", qty, 0 if use_market else order_price,
                order_type="01" if use_market else self.order_type,
            )
            if not odno:
                logger.error("[%s] 매도 주문 실패 — 다음 틱에 재시도", pos.symbol)
                return 0.0
            fill_price, filled_qty = self._resolve_fill(odno, order_price, qty)
            if filled_qty > 0:
                qty = filled_qty

        cost = self._sell_cost(fill_price, qty)
        entry = pos.entry_price
        net = self.risk.close_position(pos.symbol, fill_price, qty, cost=cost)

        # 익절 단계 플래그 갱신
        if signal == SignalType.EXIT_TP1:
            pos.tp1_done = True
        elif signal == SignalType.EXIT_TP2:
            pos.tp2_done = True

        fee = fill_price * qty * self.commission_pct / 100
        tax = fill_price * qty * self.tax_pct / 100

        self.journal.record(
            symbol      = pos.symbol,
            side        = "SELL",
            qty         = qty,
            price       = fill_price,
            entry_price = entry,
            exit_reason = reason or label,
            pnl         = net,
            fee         = fee,
            tax         = tax,
            slippage    = abs(fill_price - current_price) * qty,
            score       = pos.score,
            note        = f"odno={odno}" if odno else "DRY-RUN",
        )

        pnl_pct = (fill_price - entry) / entry * 100 if entry > 0 else 0.0

        # v3.1: 매도 시점 시장 스냅샷 기록 (AI 학습용)
        if self.ai_agent and hasattr(self.ai_agent, 'capture_market_snapshot'):
            self.ai_agent.capture_market_snapshot(
                symbol=pos.symbol, side="SELL", price=fill_price,
                score_info=f"PnL:{pnl_pct:+.2f}%",
                reason=reason or label,
            )

        emoji = '🟢' if net >= 0 else '🔴'
        self.notifier.send(
            f"{emoji} 매도 [{label}]\n"
            f"종목: {self._label(pos.symbol)}\n"
            f"{qty}주 @{fill_price:,.0f}원 ({pnl_pct:+.2f}%)\n"
            f"순손익: {net:+,.0f}원"
        )
        return net

    # ── 신호 처리 ─────────────────────────────────────────────
    def process_signal(
        self,
        pos: Position,
        indicators: SymbolIndicators,
        current_price: int,
        is_market_panic: bool = False,
    ) -> float:
        """보유 포지션 청산 신호 처리"""
        pos.update_peak(float(current_price))

        # 진입 후 경과 시간(분) — 실전은 벽시계 기준.
        # 청산 판정 입력 조립은 StrategyEngine.evaluate_exit() 공통 사용
        # (백테스트와 동일 로직 — 드리프트 방지).
        elapsed_min = None
        if pos.entry_time:
            from datetime import datetime
            now_dt = datetime.now()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%H:%M:%S"):
                try:
                    e_dt = datetime.strptime(pos.entry_time, fmt)
                    if fmt == "%H:%M:%S":
                        n_dt = datetime.strptime(now_dt.strftime("%H:%M:%S"), "%H:%M:%S")
                        elapsed_min = (n_dt - e_dt).total_seconds() / 60
                    else:
                        elapsed_min = (now_dt - e_dt).total_seconds() / 60
                    break
                except Exception:
                    pass

        signal, reason = self.strategy.evaluate_exit(
            pos, indicators, current_price,
            elapsed_min=elapsed_min, is_market_panic=is_market_panic,
        )

        if signal == SignalType.NONE:
            return 0.0

        # ARM_TP3 는 청산이 아니라 트레일링 무장
        if signal == SignalType.ARM_TP3:
            if not pos.tp3_armed:
                pos.tp3_armed = True
                logger.info("[%s] 트레일링 무장 — %s", pos.symbol, reason)
                self.notifier.send(f"🎯 {self._label(pos.symbol)} 트레일링 무장 ({reason})")
            return 0.0

        return self.exit(pos, current_price, signal, reason=reason)

    # ── 추가매수 실행 (v2.1) ───────────────────────────
    def scale_in(
        self,
        pos: Position,
        price: int,
        add_qty: int,
        reason: str = "",
        is_avg_down: bool = False,
        indicators: Optional[SymbolIndicators] = None,
        available_cash: Optional[float] = None,
    ) -> bool:
        """추가매수 주문 실행

        Returns:
            성공 여부
        """
        if add_qty <= 0:
            return False

        # 가용 현금 한도
        if available_cash is not None and available_cash > 0:
            max_qty = int(available_cash * 0.98 / price)
            if max_qty <= 0:
                logger.info("[%s] 추가매수 불가 — 현금 부족", pos.symbol)
                return False
            add_qty = min(add_qty, max_qty)

        # 최소 주문금액
        if add_qty * price < self.risk.min_order_amount:
            logger.info("[%s] 추가매수 불가 — 최소주문금액 미달", pos.symbol)
            return False

        label = "물타기" if is_avg_down else "추가매수"
        order_price = self._buy_price(price)

        logger.info(
            "[%s] %s %d주 @%d원 (%s)%s",
            pos.symbol, label, add_qty, order_price, reason,
            " [DRY-RUN]" if self.dry_run else "",
        )

        fill_price = float(order_price)
        odno = ""

        if not self.dry_run:
            odno = self._send_order(pos.symbol, "BUY", add_qty, order_price)
            if not odno:
                logger.error("[%s] %s 주문 실패", pos.symbol, label)
                return False
            fill_price, filled_qty = self._resolve_fill(odno, order_price, add_qty)
            if filled_qty > 0:
                add_qty = filled_qty

        cost = self._buy_cost(fill_price, add_qty)

        # 손절폭 재계산 (ATR 기반)
        new_stop_pct = 0.0
        new_r_unit_pct = 0.0
        if self.risk.scale_in_recalc_stop:
            atr_pct = indicators.atr_pct if indicators is not None else None
            new_stop_pct = self.strategy.resolve_stop_pct(atr_pct)
            if is_avg_down:
                # 물타기 후에는 물량이 늘어나므로 신규 평단 기준 손절폭을 타이트하게(최대 1.2%) 축소하여 손실 확대를 방지
                new_stop_pct = min(new_stop_pct, 1.2)
            new_r_unit_pct = self.strategy.resolve_r_unit_pct(new_stop_pct)

        # 포지션 갱신
        ok = self.risk.add_to_position(
            pos.symbol, fill_price, add_qty,
            cost=cost,
            is_avg_down=is_avg_down,
            new_stop_pct=new_stop_pct,
            new_r_unit_pct=new_r_unit_pct,
        )
        if not ok:
            return False

        # 일지 기록
        self.journal.record(
            symbol      = pos.symbol,
            side        = "BUY",
            qty         = add_qty,
            price       = fill_price,
            entry_price = pos.entry_price,
            exit_reason = reason,
            pnl         = 0.0,
            fee         = cost,
            tax         = 0.0,
            slippage    = abs(fill_price - price) * add_qty,
            score       = pos.score,
            note        = f"{label} odno={odno}" if odno else f"{label} DRY-RUN",
        )

        # 알림
        emoji = "🟢" if not is_avg_down else "🟡"
        self.notifier.send(
            f"{emoji} {label}\n"
            f"종목: {self._label(pos.symbol)}\n"
            f"+{add_qty}주 @{fill_price:,.0f}원\n"
            f"총 {pos.qty}주 평단 {pos.entry_price:,.0f}원\n"
            f"사유: {reason}"
        )
        return True

    # ── 전 포지션 강제 청산 ──────────────────────────────────
    def force_close_all(self) -> None:
        """장 마감 전 시장가 강제 청산"""
        symbols = list(self.risk.positions.keys())
        if not symbols:
            return

        logger.info("강제 청산 시작: %d 종목", len(symbols))
        for sym in symbols:
            pos = self.risk.positions.get(sym)
            if not pos:
                continue
            try:
                price = self.client.get_last_price(sym) or int(pos.entry_price)
                self.exit(
                    pos, price, SignalType.EXIT_FORCE,
                    reason="장마감 강제청산", market_order=True,
                )
            except Exception as e:
                logger.error("[%s] 강제 청산 실패: %s", sym, e)
                self.notifier.send(f"⚠️ {self._label(sym)} 강제청산 실패 — 수동 확인 필요\n{e}")

    # ── 분할 강제 청산 (v2.0) ────────────────────────────────
    def force_close_partial(self, ratio: float) -> None:
        """전 종목의 잔량 중 ratio 비율만 시장가 청산

        전 종목을 15:15에 동시 시장가로 던지면 슬리피지가 크다.
        15:08부터 나눠 던져 체결 충격을 분산한다.

        올림(-(-x//1)) 처리로 최소 1주는 반드시 나가게 해,
        잔량이 적을 때 분할이 무의미해지는 것을 막는다.
        """
        ratio = min(1.0, max(0.0, float(ratio)))
        if ratio <= 0:
            return

        symbols = list(self.risk.positions.keys())
        if not symbols:
            return

        logger.info("분할 청산 시작: %d 종목 × 잔량 %.0f%%", len(symbols), ratio * 100)
        for sym in symbols:
            pos = self.risk.positions.get(sym)
            if not pos or pos.qty <= 0:
                continue
            qty = min(pos.qty, max(1, int(-(-pos.qty * ratio // 1))))
            try:
                price = self.client.get_last_price(sym) or int(pos.entry_price)
                self.exit(
                    pos, price, SignalType.EXIT_FORCE,
                    reason=f"장마감 분할청산({ratio*100:.0f}%)",
                    market_order=True, qty_override=qty,
                )
            except Exception as e:
                logger.error("[%s] 분할 청산 실패: %s", sym, e)
                self.notifier.send(f"⚠️ {self._label(sym)} 분할청산 실패 — 최종 슬롯에서 재시도\n{e}")

    # ── 내부 헬퍼 ─────────────────────────────────────────────
    def _buy_price(self, price: int) -> int:
        return round_to_tick(price * (1 + self.slippage_bps / 10000), price)

    def _sell_price(self, price: int) -> int:
        return round_to_tick(price * (1 - self.slippage_bps / 10000), price)

    def _send_order(self, symbol: str, side: str, qty: int, price: int,
                    order_type: Optional[str] = None) -> str:
        """주문 전송 — 이중 체결 방지 로직 포함

        주문 API는 멱등하지 않으므로 맹목적 재시도를 하지 않는다.
        네트워크 오류로 응답이 유실된 경우에만, 주문이 실제로 접수되었는지
        확인한 뒤 접수되지 않았을 때만 1회 재전송한다.
        """
        ot = order_type if order_type is not None else self.order_type
        try:
            resp = self.client.place_order(symbol, side, qty, price, ot)
            odno = str(resp.get("output", {}).get("ODNO", "")) or "UNKNOWN"
            logger.info("주문 접수 [%s] odno=%s", symbol, odno)
            return odno

        except KISAPIError as e:
            # 업무 오류(잔고부족·호가이탈 등)는 재시도해도 동일 실패
            logger.error("[%s] 주문 거부: %s", symbol, e)
            return ""

        except Exception as e:
            logger.warning("[%s] 주문 응답 유실: %s — 착지 여부 확인", symbol, e)
            time.sleep(1.0)
            landed = self.client.verify_order_landed(symbol, side, qty)
            if landed:
                return landed

            try:
                resp = self.client.place_order(symbol, side, qty, price, ot)
                odno = str(resp.get("output", {}).get("ODNO", "")) or "UNKNOWN"
                logger.info("주문 재전송 성공 [%s] odno=%s", symbol, odno)
                return odno
            except Exception as e2:
                logger.error("[%s] 주문 재전송 실패: %s", symbol, e2)
                return ""

    def _resolve_fill(self, odno: str, fallback_price: int, qty: int, symbol: str = "") -> tuple[float, int]:
        """실제 체결가·체결수량 확인 및 미체결 잔여분 자동 취소

        Returns:
            (체결가, 체결수량) — 확인 실패 시 (주문가, 0)
        """
        if not self.confirm_fill or odno in ("", "UNKNOWN"):
            return float(fallback_price), qty if self.dry_run else 0

        time.sleep(self.fill_wait_sec)
        price, filled = self.client.get_fill_price(odno, fallback=float(fallback_price))

        unfilled = qty - filled
        if unfilled > 0 and not self.dry_run:
            logger.warning("odno=%s 미체결 잔여 %d주 발견 → KIS 주문 취소 전송", odno, unfilled)
            try:
                self.client.cancel_order(odno, unfilled, symbol=symbol)
            except Exception as e:
                logger.error("odno=%s 주문 취소 실패: %s", odno, e)

        if filled == 0:
            logger.warning("odno=%s 전량 미체결 취소 완료", odno)
        elif filled < qty:
            logger.info("odno=%s 부분체결 %d/%d주 (잔여 %d주 취소 완료)", odno, filled, qty, unfilled)

        return price, filled
