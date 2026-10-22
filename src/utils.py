"""
utils.py — 로거 / 시간 / 호가단위 유틸

v1.1 수정사항
  - 휴장일(공휴일) 캘린더 추가 — 휴장일 실행 방지
  - 로그 파일 일자별 자동 롤오버
  - round_to_tick 이 매수/매도 방향에 따라 올림/내림 선택 가능
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

# ──────────────────────────────────────────────
#  KRX 휴장일 (한국거래소 공휴일)
#  연도별 표로 관리한다. 매년 말 다음 해 표를 추가할 것.
#  근거: 관공서 공휴일 규정·해당 연도 월력요항, KRX 관례(근로자의 날·
#        12/31 연말 휴장). 임시공휴일·선거일은 지정되는 즉시 추가한다.
#  표에 없는 연도를 조회하면 경고를 남긴다 (is_holiday 참조).
# ──────────────────────────────────────────────
_holiday_logger = logging.getLogger("autotrader.utils")

KRX_HOLIDAYS_BY_YEAR: dict[int, set[str]] = {
    2026: {
        "20260101",  # 신정
        "20260216", "20260217", "20260218",  # 설날
        "20260301", "20260302",  # 삼일절(대체)
        "20260501",  # 근로자의 날 (KRX 휴장)
        "20260505",  # 어린이날
        "20260524", "20260525",  # 부처님오신날(대체)
        "20260603",  # 지방선거
        "20260606",  # 현충일
        "20260717",  # 제헌절 (2026년 공휴일 재지정)
        "20260815", "20260817",  # 광복절(대체)
        "20260924", "20260925", "20260926",  # 추석
        "20261003", "20261005",  # 개천절(대체)
        "20261009",  # 한글날
        "20261225",  # 성탄절
        "20261231",  # 연말 휴장
    },
    2027: {
        "20270101",  # 신정
        "20270206", "20270207", "20270208", "20270209",  # 설날(일)·다음날·대체
        "20270301",  # 삼일절
        "20270501", "20270503",  # 노동절(토)·대체
        "20270505",  # 어린이날
        "20270513",  # 부처님오신날
        "20270606",  # 현충일(일, 대체 없음)
        "20270717", "20270719",  # 제헌절(토)·대체
        "20270815", "20270816",  # 광복절(일)·대체
        "20270914", "20270915", "20270916",  # 추석
        "20271003", "20271004",  # 개천절(일)·대체
        "20271009", "20271011",  # 한글날(토)·대체
        "20271225", "20271227",  # 성탄절(토)·대체
        "20271231",  # 연말 휴장
    },
}

# 하위 호환: 전체 연도 합집합
KRX_HOLIDAYS: set[str] = set().union(*KRX_HOLIDAYS_BY_YEAR.values())

_warned_holiday_years: set[int] = set()


# ──────────────────────────────────────────────
#  로거
# ──────────────────────────────────────────────
def setup_logger(
    name: str = "autotrader",
    level: str = "INFO",
    log_dir: str = "logs",
) -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)-22s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # 자정 기준 일별 롤오버, 30일 보관
    fh = TimedRotatingFileHandler(
        os.path.join(log_dir, "autotrader.log"),
        when="midnight", interval=1, backupCount=30, encoding="utf-8",
    )
    fh.suffix = "%Y%m%d"
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


# ──────────────────────────────────────────────
#  시간
# ──────────────────────────────────────────────
def now_kst() -> datetime:
    return datetime.now(tz=KST)


def now_time_kst() -> time:
    return now_kst().time()


def now_hhmm() -> str:
    """현재 시각 'HHMM'"""
    return now_kst().strftime("%H%M")


def now_hhmmss() -> str:
    return now_kst().strftime("%H%M%S")


def str_to_time(s: str) -> time:
    """'HH:MM' → time 객체"""
    s = s.replace(":", "")
    return time(int(s[:2]), int(s[2:4]))


def is_holiday(d: date | None = None) -> bool:
    """KRX 휴장일 여부

    휴장일 표(KRX_HOLIDAYS_BY_YEAR)에 없는 연도는 휴장일을 알 수 없으므로
    연도당 1회 경고를 남긴다. 표 누락이 조용히 지나가 공휴일에 봇이
    도는 사고를 막기 위한 장치이니, 경고가 보이면 표부터 갱신할 것.
    """
    d = d or now_kst().date()
    if d.year not in KRX_HOLIDAYS_BY_YEAR and d.year not in _warned_holiday_years:
        _warned_holiday_years.add(d.year)
        _holiday_logger.warning(
            "KRX 휴장일 표에 %d년이 없습니다 — 공휴일을 영업일로 오판할 수 있으니 "
            "src/utils.py의 KRX_HOLIDAYS_BY_YEAR를 갱신하세요.", d.year,
        )
    return d.strftime("%Y%m%d") in KRX_HOLIDAYS


def is_trading_day(d: date | None = None) -> bool:
    """영업일 여부 (평일 + 비휴장일)"""
    d = d or now_kst().date()
    return d.weekday() < 5 and not is_holiday(d)


def is_market_open() -> bool:
    """정규장 시간 여부 (09:00~15:30 KST, 영업일)"""
    t = now_kst()
    if not is_trading_day(t.date()):
        return False
    return time(9, 0) <= t.time() <= time(15, 30)


def time_between(start: str, end: str) -> bool:
    """현재 시각이 start~end 사이인지 ('HH:MM')"""
    cur = now_time_kst()
    return str_to_time(start) <= cur <= str_to_time(end)


def hhmm_ge(target: str) -> bool:
    """현재 시각이 target('HH:MM') 이후인지"""
    return now_hhmm() >= target.replace(":", "")


# ──────────────────────────────────────────────
#  호가 단위
# ──────────────────────────────────────────────
def tick_size(price: float) -> int:
    """KRX 호가단위 (2023.01 개정 기준)"""
    p = int(price)
    if p < 2_000:
        return 1
    elif p < 5_000:
        return 5
    elif p < 20_000:
        return 10
    elif p < 50_000:
        return 50
    elif p < 200_000:
        return 100
    elif p < 500_000:
        return 500
    else:
        return 1_000


def round_to_tick(price: float, base_price: float | None = None,
                  mode: str = "down") -> int:
    """가격을 호가단위로 정렬

    Args:
        mode: 'down'(내림) / 'up'(올림) / 'near'(반올림)
    """
    ts = tick_size(base_price if base_price else price)
    if mode == "up":
        return int(-(-price // ts) * ts)
    elif mode == "near":
        return int(round(price / ts) * ts)
    return int(price // ts) * ts


# ──────────────────────────────────────────────
#  포맷
# ──────────────────────────────────────────────
def fmt_pct(v: float) -> str:
    return f"{v:+.2f}%"


def fmt_krw(v: float) -> str:
    return f"{v:,.0f}원"
