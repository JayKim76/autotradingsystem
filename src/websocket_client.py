"""
websocket_client.py — KIS 실시간 체결/호가 WebSocket 클라이언트

메인 루프(AutoTrader)는 RealtimePriceFeed 로 이 모듈을 사용한다.
피드가 없거나 데이터가 오래되면 REST 폴링으로 자동 대체(fallback)되므로
WebSocket 장애가 매매를 멈추지는 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Callable, Any, Optional, Union

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

    async def _watch_symbols(self, resolve, on_trade, on_orderbook) -> None:
        """구독 대상 변경 감시 — 종목이 바뀌면 재연결 없이 구독만 갱신"""
        while self._running and self._ws is not None:
            await asyncio.sleep(2)
            try:
                desired = set(resolve())
            except Exception:
                continue
            try:
                for sym in desired - self._subscribed:
                    if on_trade:
                        await self._send_subscribe(TR_CCLD, sym)
                    if on_orderbook:
                        await self._send_subscribe(TR_ASKP, sym)
                    self._subscribed.add(sym)
                for sym in list(self._subscribed - desired):
                    if on_trade:
                        await self._send_unsubscribe(TR_CCLD, sym)
                    if on_orderbook:
                        await self._send_unsubscribe(TR_ASKP, sym)
                    self._subscribed.discard(sym)
            except Exception as e:
                logger.debug("구독 갱신 실패: %s", e)

    async def subscribe(
        self,
        symbols: Union[list[str], Callable[[], list[str]]],
        on_trade: Callable[[dict], None] | None = None,
        on_orderbook: Callable[[dict], None] | None = None,
    ) -> None:
        """실시간 구독 시작 (비동기 루프, 연결 끊기면 자동 재연결)

        symbols에 callable을 주면 구독 대상을 주기적으로 다시 읽어
        추적 종목 변경에 재연결 없이 대응한다.
        """
        resolve = symbols if callable(symbols) else (lambda: list(symbols))
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=30) as ws:
                    self._ws = ws
                    logger.info("WebSocket 연결: %s", self.ws_url)

                    self._subscribed = set()
                    for sym in resolve():
                        if on_trade:
                            await self._send_subscribe(TR_CCLD, sym)
                        if on_orderbook:
                            await self._send_subscribe(TR_ASKP, sym)
                        self._subscribed.add(sym)

                    watcher = asyncio.ensure_future(
                        self._watch_symbols(resolve, on_trade, on_orderbook)
                    )
                    try:
                        await self._consume(ws, on_trade, on_orderbook)
                    finally:
                        watcher.cancel()

            except (websockets.exceptions.ConnectionClosed, OSError) as e:
                if self._running:
                    logger.warning("WebSocket 연결 끊김 (%s), 5초 후 재연결...", e)
                    await asyncio.sleep(5)
                else:
                    break

    async def _consume(self, ws, on_trade, on_orderbook) -> None:
        """수신 메시지 디스패치 루프"""
        async for message in ws:
            if not self._running:
                break
            if isinstance(message, bytes):
                message = message.decode("utf-8")

            # 실시간 데이터 (파이프 포맷)
            if message.startswith("0|") or message.startswith("1|"):
                parts = message.split("|")
                if len(parts) >= 2:
                    tr_id = parts[1]
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

    def stop(self) -> None:
        """구독 중단"""
        self._running = False


class RealtimePriceFeed:
    """백그라운드 스레드로 실시간 체결/호가를 캐시하는 피드

    메인 루프는 get_price()/get_orderbook_totals()로 최신값을 읽고,
    없거나 오래됐으면(max_age_sec 초과) None을 받아 REST 폴링으로 대체한다.
    피드 장애는 예외를 전파하지 않는다 — 매매가 멈추지 않는 쪽이 우선이다.
    """

    def __init__(self, auth: KISAuth, max_age_sec: float = 10.0) -> None:
        self._client = KISWebSocket(auth)
        self.max_age_sec = float(max_age_sec)
        self._lock = threading.Lock()
        self._prices: dict[str, tuple[int, float]] = {}        # symbol -> (price, monotonic)
        self._books: dict[str, tuple[int, int, float]] = {}    # symbol -> (ask, bid, monotonic)
        self._symbols: set[str] = set()
        self._thread: Optional[threading.Thread] = None

    # ── 구독 대상 관리 ────────────────────────────────────────
    def set_symbols(self, symbols) -> None:
        with self._lock:
            self._symbols = set(symbols)

    def _desired(self) -> list[str]:
        with self._lock:
            return sorted(self._symbols)

    # ── 생명주기 ──────────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="kis-realtime-feed",
        )
        self._thread.start()
        logger.info("실시간 피드 시작 (max_age=%.0fs)", self.max_age_sec)

    def _run(self) -> None:
        try:
            asyncio.run(self._client.subscribe(
                self._desired,
                on_trade=self._on_trade,
                on_orderbook=self._on_orderbook,
            ))
        except Exception as e:
            logger.warning("실시간 피드 종료: %s — REST 폴링으로 대체됩니다", e)

    def stop(self) -> None:
        self._client.stop()

    # ── 콜백 (WS 스레드에서 호출) ─────────────────────────────
    def _on_trade(self, d: dict) -> None:
        symbol = d.get("symbol")
        price = d.get("price") or 0
        if symbol and price > 0:
            with self._lock:
                self._prices[symbol] = (int(price), time.monotonic())

    def _on_orderbook(self, d: dict) -> None:
        symbol = d.get("symbol")
        if symbol:
            with self._lock:
                self._books[symbol] = (
                    int(d.get("ask_total", 0)), int(d.get("bid_total", 0)),
                    time.monotonic(),
                )

    # ── 조회 (메인 루프에서 호출) ─────────────────────────────
    def _fresh(self, ts: float) -> bool:
        return (time.monotonic() - ts) <= self.max_age_sec

    def get_price(self, symbol: str) -> Optional[int]:
        with self._lock:
            item = self._prices.get(symbol)
        if item and self._fresh(item[1]):
            return item[0]
        return None

    def get_orderbook_totals(self, symbol: str) -> Optional[tuple[int, int]]:
        with self._lock:
            item = self._books.get(symbol)
        if item and self._fresh(item[2]):
            return item[0], item[1]
        return None


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
