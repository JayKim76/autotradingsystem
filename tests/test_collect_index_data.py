"""지수 데이터 수집기의 금리→가격 변환 테스트"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "collect_index_data",
    Path(__file__).parent.parent / "scripts" / "collect_index_data.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
yield_to_price_series = _mod.yield_to_price_series
parse_fred_csv = _mod.parse_fred_csv
parse_generic_csv = _mod.parse_generic_csv
merge_gold_series = _mod.merge_gold_series


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


def test_parse_fred_csv_skips_missing():
    text = "DATE,GOLDAMGBD228NLBM\n1990-01-02,402.70\n1990-01-03,.\n1990-01-04,394.20\n"
    rows = parse_fred_csv(text)
    assert [r[0] for r in rows] == ["19900102", "19900104"]
    assert rows[0][4] == 402.70


def test_parse_generic_csv_stooq_format():
    text = ("Date,Open,High,Low,Close,Volume\n"
            "1990-01-02,401.00,403.00,400.00,402.70,0\n"
            "1990-01-03,402.00,404.00,401.00,403.10,0\n")
    rows = parse_generic_csv(text)
    assert len(rows) == 2 and rows[0][0] == "19900102" and rows[1][4] == 403.10


def test_merge_gold_series_cutoff():
    early = [("19900102", 1, 1, 1, 400.0), ("20000829", 1, 1, 1, 280.0),
             ("20000901", 1, 1, 1, 999.0)]  # 선물 시작 이후 현물은 버려진다
    late = [("20000830", 1, 1, 1, 273.9), ("20121231", 1, 1, 1, 1674.8)]
    merged = merge_gold_series(early, late)
    assert [r[0] for r in merged] == ["19900102", "20000829", "20000830", "20121231"]


def test_yield_conversion_uses_previous_yield():
    # 첫 구간의 이자는 첫 금리(첫날 금리)가 아니라 "전일 금리"로 붙는다:
    # 둘째 날 가격은 첫날 금리로 계산돼야 한다
    rows = [("19900102", 36.0), ("19900103", 0.0)]
    prices = yield_to_price_series(rows)
    assert abs(prices[1][1] - 100.0 * (1 + 0.36 * 1 / 360)) < 1e-9
