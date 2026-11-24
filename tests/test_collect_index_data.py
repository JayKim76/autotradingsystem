"""지수 데이터 수집기의 금리→가격 변환 테스트"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "collect_index_data",
    Path(__file__).parent.parent / "scripts" / "collect_index_data.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
yield_to_price_series = _mod.yield_to_price_series


def test_yield_conversion_accrues():
    # 금리 3.6% 고정, 10일 간격이면 회당 3.6% × 10/360 = 0.1% 누적
    rows = [("19900102", 3.6), ("19900112", 3.6), ("19900122", 3.6)]
    prices = yield_to_price_series(rows)
    assert prices[0] == ("19900102", 100.0)
    assert abs(prices[1][1] - 100.1) < 1e-9
    assert abs(prices[2][1] - 100.1 * 1.001) < 1e-9


def test_yield_conversion_zero_rate_flat():
    rows = [("19900102", 0.0), ("19900201", 0.0)]
    prices = yield_to_price_series(rows)
    assert all(p == 100.0 for _, p in prices)


def test_yield_conversion_uses_previous_yield():
    # 첫 구간의 이자는 첫 금리(첫날 금리)가 아니라 "전일 금리"로 붙는다:
    # 둘째 날 가격은 첫날 금리로 계산돼야 한다
    rows = [("19900102", 36.0), ("19900103", 0.0)]
    prices = yield_to_price_series(rows)
    assert abs(prices[1][1] - 100.0 * (1 + 0.36 * 1 / 360)) < 1e-9
