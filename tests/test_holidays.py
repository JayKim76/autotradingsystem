"""KRX 휴장일 표 회귀 테스트 (2026 누락분 + 2027 신규)"""
from datetime import date

from src.utils import KRX_HOLIDAYS_BY_YEAR, is_holiday, is_trading_day


def test_holiday_years_covered():
    assert 2026 in KRX_HOLIDAYS_BY_YEAR
    assert 2027 in KRX_HOLIDAYS_BY_YEAR


def test_2026_missing_weekday_holidays_added():
    assert is_holiday(date(2026, 5, 1)) is True   # 근로자의 날 (KRX 휴장)
    assert is_holiday(date(2026, 7, 17)) is True  # 제헌절 (공휴일 재지정)


def test_2027_holidays():
    for d in [
        date(2027, 1, 1),    # 신정
        date(2027, 2, 8),    # 설 연휴
        date(2027, 2, 9),    # 설 대체공휴일
        date(2027, 3, 1),    # 삼일절
        date(2027, 5, 3),    # 노동절 대체 (5/1 토)
        date(2027, 5, 5),    # 어린이날
        date(2027, 5, 13),   # 부처님오신날
        date(2027, 7, 19),   # 제헌절 대체 (7/17 토)
        date(2027, 8, 16),   # 광복절 대체
        date(2027, 9, 14),   # 추석 연휴
        date(2027, 9, 15),   # 추석
        date(2027, 9, 16),   # 추석 연휴
        date(2027, 10, 4),   # 개천절 대체
        date(2027, 10, 11),  # 한글날 대체 (10/9 토)
        date(2027, 12, 27),  # 성탄절 대체 (12/25 토)
        date(2027, 12, 31),  # 연말 휴장
    ]:
        assert is_holiday(d) is True, d


def test_2027_regular_weekdays_are_trading_days():
    # 현충일(6/6 일)은 대체공휴일 대상이 아니므로 다음 월요일은 개장일
    assert is_holiday(date(2027, 6, 7)) is False
    assert is_trading_day(date(2027, 6, 7)) is True
    assert is_trading_day(date(2027, 2, 10)) is True
    # 휴장일은 거래일이 아니다
    assert is_trading_day(date(2027, 9, 15)) is False
