"""
websocket_client.py — KIS 실시간 체결/호가 WebSocket 클라이언트
메인 루프는 REST 폴링으로 동작하며, 이 모듈은 선택적 확장용입니다.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Any

import websockets

from .auth import KISAuth

logger = logging.getLogger("autotrader.ws")

_PAPER_WS = "ws://ops.koreainvestment.com:31000"
_REAL_WS  = "ws://ops.koreainvestment.com:21000"

# TR_ID 상수
TR_CCLD  = "H0STCNT0"   # 주식 체결
TR_ASKP  = "H0STASP0"   # 주식 호가


class KISWebSocket:
    """KIS 실시간 WebSocket 클라이언트

    사용 예::

        async def on_trade(data):
            print(data)

        ws = KISWebSocket(auth)
        await ws.subscribe(["005930"], on_trade=on_trade)
    """

    def __init__(self, auth: KISAuth) -> None:
        self.auth = auth
        self.ws_url = _PAPER_WS if auth.mode != "real" else _REAL_WS
        self._ws: Any = None
        self._subscribed: set[str] = set()
        self._running = False

    async def _send_subscribe(self, tr_id: str, symbol: str) -> None:
        payload = {
            "header": {
                "approval_key": self.auth.get_approval_key(),
                "custtype":     "P",
                "tr_type":      "1",   # 등록
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id":    tr_id,
                    "tr_key":   symbol,
                }
            },
        }
        await self._ws.send(json.dumps(payload))
        logger.debug("구독 등록: %s %s", tr_id, symbol)

    async def _send_unsubscribe(self, tr_id: str, symbol: str) -> None:
        payload = {
            "header": {
                "approval_key": self.auth.get_approval_key(),
                "custtype":     "P",
                "tr_type":      "2",   # 해제
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id":  tr_id,
                    "tr_key": symbol,
                }
            },
        }
        await self._ws.send(json.dumps(payload))

    @staticmethod
    def _parse_ccld(raw: str) -> dict:
        """체결 데이터 파싱 (파이프 구분 압축 포맷)"""
        parts = raw.split("|")
        if len(parts) < 4:
            return {}
        fields = parts[3].split("^")
        if len(fields) < 13:
            return {}
        return {
            "symbol":    fields[0],
            "time":      fields[1],
            "price":     int(fields[2]),
            "volume":    int(fields[9]),
            "cum_vol":   int(fields[13]) if len(fields) > 13 else 0,
            "change_pct": float(fields[5]) if fields[5] else 0.0,
        }

    @staticmethod
    def _parse_askp(raw: str) -> dict:
        """호가 데이터 파싱"""
        parts = raw.split("|")
        if len(parts) < 4:
            return {}
        fields = parts[3].split("^")
        if len(fields) < 50:
            return {}
        try:
            ask_total = sum(int(fields[30 + i]) for i in range(10))
            bid_total = sum(int(fields[40 + i]) for i in range(10))
        except (ValueError, IndexError):
            ask_total, bid_total = 0, 0
        return {
            "symbol":    fields[0],
            "ask_total": ask_total,
            "bid_total": bid_total,
        }

    async def subscribe(
        self,
        symbols: list[str],
        on_trade: Callable[[dict], None] | None = None,
        on_orderbook: Callable[[dict], None] | None = None,
    ) -> None:
        """실시간 구독 시작 (비동기 루프, 연결 끊기면 자동 재연결)"""
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=30) as ws:
                    self._ws = ws
                    logger.info("WebSocket 연결: %s", self.ws_url)

                    for sym in symbols:
                        if on_trade:
                            await self._send_subscribe(TR_CCLD, sym)
                        if on_orderbook:
                            await self._send_subscribe(TR_ASKP, sym)
                        self._subscribed.add(sym)

                    async for message in ws:
                        if not self._running:
                            break
                        if isinstance(message, bytes):
                            message = message.decode("utf-8")

                        # PINGPONG 처리
                        if message.startswith("0|") or message.startswith("1|"):
                            parts = message.split("|")
                            if len(parts) >= 2:
                                tr_id = parts[1] if len(parts) > 1 else ""
                                if tr_id == TR_CCLD and on_trade:
                                    parsed = self._parse_ccld(message)
                                    if parsed:
                                        on_trade(parsed)
                                elif tr_id == TR_ASKP and on_orderbook:
                                    parsed = self._parse_askp(message)
                                    if parsed:
                                        on_orderbook(parsed)
                        else:
                            # JSON 응답 (구독 확인 등)
                            try:
                                data = json.loads(message)
                                logger.debug("WS 응답: %s", data.get("header", {}).get("tr_id"))
                            except json.JSONDecodeError:
                                pass

            except (websockets.exceptions.ConnectionClosed, OSError) as e:
                if self._running:
                    logger.warning("WebSocket 연결 끊김 (%s), 5초 후 재연결...", e)
                    await asyncio.sleep(5)
                else:
                    break

    def stop(self) -> None:
        """구독 중단"""
        self._running = False


# ── 데모 실행 ─────────────────────────────────────────────────
async def demo_run(auth: KISAuth, symbols: list[str]) -> None:
    """WebSocket 데모 (체결 + 호가 출력)"""
    ws = KISWebSocket(auth)

    def on_trade(d: dict) -> None:
        print(f"[체결] {d['symbol']} {d['price']:,}원 {d['volume']:,}주")

    def on_orderbook(d: dict) -> None:
        print(f"[호가] {d['symbol']} 매수잔량={d['bid_total']:,} 매도잔량={d['ask_total']:,}")

    try:
        await ws.subscribe(symbols, on_trade=on_trade, on_orderbook=on_orderbook)
    except KeyboardInterrupt:
        ws.stop()
        print("WebSocket 종료")
