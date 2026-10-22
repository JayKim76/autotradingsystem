"""RealtimePriceFeed 캐시 동작 테스트 (네트워크 없이 콜백 주입)"""
import time

from src.websocket_client import RealtimePriceFeed


class _FakeAuth:
    mode = "paper"


def _feed(max_age=10.0):
    return RealtimePriceFeed(_FakeAuth(), max_age_sec=max_age)


def test_trade_price_cached_and_served():
    feed = _feed()
    feed._on_trade({"symbol": "005930", "price": 70500})
    assert feed.get_price("005930") == 70500
    assert feed.get_price("000660") is None


def test_stale_price_returns_none():
    feed = _feed(max_age=0.05)
    feed._on_trade({"symbol": "005930", "price": 70500})
    time.sleep(0.08)
    assert feed.get_price("005930") is None  # 오래되면 REST 폴링으로 대체돼야 함


def test_orderbook_totals_cached():
    feed = _feed()
    feed._on_orderbook({"symbol": "005930", "ask_total": 1200, "bid_total": 3400})
    assert feed.get_orderbook_totals("005930") == (1200, 3400)


def test_symbols_management():
    feed = _feed()
    feed.set_symbols(["005930", "000660"])
    assert feed._desired() == ["000660", "005930"]
    feed.set_symbols([])
    assert feed._desired() == []


def test_parse_ccld_roundtrip_format():
    from src.websocket_client import KISWebSocket
    fields = ["005930", "093015", "70500", "0", "0", "1.20", "0", "70600", "70000", "120", "0", "0", "0", "550000"]
    raw = "0|H0STCNT0|001|" + "^".join(fields)
    parsed = KISWebSocket._parse_ccld(raw)
    assert parsed["symbol"] == "005930"
    assert parsed["price"] == 70500
    assert parsed["volume"] == 120
