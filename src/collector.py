"""
collector.py — 과거 분봉 데이터 수집기

KIS 분봉 API(FHKST03010200)는 **당일 데이터만** 반환한다.
따라서 과거 데이터로 백테스트하려면 매 영업일 장 마감 후 수집해 축적해야 한다.
오늘부터 매일 돌리면 데이터가 쌓인다.

저장 구조::

    data/candles/
      ├── 20260728/
      │   ├── 005930.csv
      │   └── 247540.csv
      └── 20260729/
          └── ...

CSV 포맷: time,open,high,low,close,volume  (1분봉, time='HHMMSS')

사용::

    python -m src.collector                      # 기본 워치리스트 수집
    python -m src.collector --symbols 005930,000660
    python -m src.collector --from-screener      # 당일 스크리닝 통과 종목만
    python -m src.collector --universe-file watchlist.txt
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
logger = logging.getLogger("autotrader.collector")

CSV_HEADER = ["time", "open", "high", "low", "close", "volume"]


class CandleCollector:
    """일자별 분봉 수집·저장"""

    def __init__(self, client, base_dir: str = "data/candles") -> None:
        self.client = client
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    # ── 경로 ──────────────────────────────────────────────────
    def _day_dir(self, date_str: str) -> Path:
        d = self.base / date_str
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path_for(self, symbol: str, date_str: str) -> Path:
        return self._day_dir(date_str) / f"{symbol}.csv"

    # ── 수집 ──────────────────────────────────────────────────
    def collect_symbol(self, symbol: str, date_str: Optional[str] = None,
                       overwrite: bool = False) -> int:
        """단일 종목 당일 분봉 수집 → CSV 저장

        Returns:
            저장된 봉 개수 (실패 시 0)
        """
        date_str = date_str or datetime.now(tz=KST).strftime("%Y%m%d")
        out = self.path_for(symbol, date_str)

        if out.exists() and not overwrite:
            logger.info("[%s] 이미 수집됨 — 건너뜀 (%s)", symbol, out.name)
            return 0

        try:
            rows = self.client.get_minute_candles_full(symbol)
        except Exception as e:
            logger.error("[%s] 분봉 조회 실패: %s", symbol, e)
            return 0

        if not rows:
            logger.warning("[%s] 분봉 데이터 없음", symbol)
            return 0

        # 파싱 + 정렬 + 중복 제거
        seen: dict[str, list] = {}
        for r in rows:
            try:
                t = str(r.get("stck_cntg_hour", "")).strip()
                if not t or len(t) < 6:
                    continue
                o = float(r.get("stck_oprc", 0))
                h = float(r.get("stck_hgpr", 0))
                l = float(r.get("stck_lwpr", 0))
                c = float(r.get("stck_prpr", 0))
                v = int(float(r.get("cntg_vol", 0)))
            except (TypeError, ValueError):
                continue

            if h <= 0 or l <= 0 or c <= 0 or h < l:
                continue
            seen[t] = [t, int(o), int(h), int(l), int(c), v]

        if not seen:
            logger.warning("[%s] 유효 봉 없음", symbol)
            return 0

        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(CSV_HEADER)
            for t in sorted(seen):
                w.writerow(seen[t])

        logger.info("[%s] %d봉 저장 → %s", symbol, len(seen), out)
        return len(seen)

    def collect(self, symbols: list[str], date_str: Optional[str] = None,
                overwrite: bool = False) -> dict[str, int]:
        """여러 종목 일괄 수집"""
        date_str = date_str or datetime.now(tz=KST).strftime("%Y%m%d")
        logger.info("수집 시작: %d종목 (%s)", len(symbols), date_str)

        results: dict[str, int] = {}
        for sym in symbols:
            results[sym] = self.collect_symbol(sym, date_str, overwrite)

        ok = sum(1 for v in results.values() if v > 0)
        total = sum(results.values())
        logger.info("수집 완료: %d/%d종목 성공, 총 %d봉", ok, len(symbols), total)
        return results

    # ── 조회 ──────────────────────────────────────────────────
    def available_dates(self) -> list[str]:
        """수집된 날짜 목록"""
        return sorted(
            d.name for d in self.base.iterdir()
            if d.is_dir() and d.name.isdigit() and len(d.name) == 8
        )

    def available_symbols(self, date_str: str) -> list[str]:
        d = self.base / date_str
        if not d.exists():
            return []
        return sorted(f.stem for f in d.glob("*.csv"))

    def coverage(self) -> dict:
        """수집 현황 요약"""
        dates = self.available_dates()
        if not dates:
            return {"days": 0, "symbols": [], "range": None, "files": 0}

        symbol_days: dict[str, int] = {}
        files = 0
        for d in dates:
            for s in self.available_symbols(d):
                symbol_days[s] = symbol_days.get(s, 0) + 1
                files += 1

        return {
            "days":    len(dates),
            "range":   (dates[0], dates[-1]),
            "symbols": sorted(symbol_days.items(), key=lambda x: -x[1]),
            "files":   files,
        }

    def print_coverage(self) -> None:
        c = self.coverage()
        print("═" * 52)
        print("  수집 현황")
        print("═" * 52)
        if c["days"] == 0:
            print("  수집된 데이터가 없습니다.")
            print("  python -m src.collector 로 수집을 시작하세요.")
            print("═" * 52)
            return

        print(f"  기간      : {c['range'][0]} ~ {c['range'][1]}")
        print(f"  영업일    : {c['days']}일")
        print(f"  파일 수   : {c['files']}개")
        print("─" * 52)
        print("  종목별 수집 일수 (상위 15)")
        for sym, days in c["symbols"][:15]:
            bar = "█" * min(30, days)
            print(f"    {sym}  {days:3d}일  {bar}")
        print("═" * 52)

        if c["days"] < 60:
            print(f"  ⚠️ 백테스트 신뢰도를 위해 최소 60영업일(약 3개월) 권장")
            print(f"     현재 {c['days']}일 — {60 - c['days']}일 더 필요")
            print("═" * 52)


# ──────────────────────────────────────────────
#  로더 (백테스트용)
# ──────────────────────────────────────────────
def load_range(
    base_dir: str = "data/candles",
    start: Optional[str] = None,
    end: Optional[str] = None,
    symbols: Optional[list[str]] = None,
) -> dict[str, dict[str, list]]:
    """날짜 범위의 분봉을 로드

    Returns:
        {날짜: {종목: [Candle, ...]}}
    """
    from .backtest import load_candles_csv

    base = Path(base_dir)
    if not base.exists():
        return {}

    out: dict[str, dict[str, list]] = {}
    for day_dir in sorted(base.iterdir()):
        if not (day_dir.is_dir() and day_dir.name.isdigit()):
            continue
        date_str = day_dir.name
        if start and date_str < start:
            continue
        if end and date_str > end:
            continue

        day_data: dict[str, list] = {}
        for f in sorted(day_dir.glob("*.csv")):
            if symbols and f.stem not in symbols:
                continue
            candles = load_candles_csv(f)
            if candles:
                day_data[f.stem] = candles

        if day_data:
            out[date_str] = day_data

    return out


# ──────────────────────────────────────────────
#  CLI
# ──────────────────────────────────────────────
def _default_watchlist() -> list[str]:
    from .screener import default_universe
    return default_universe()


def main() -> None:
    from dotenv import load_dotenv
    from .utils import setup_logger, is_trading_day
    from .auth import KISAuth
    from .kis_client import KISClient

    p = argparse.ArgumentParser(description="KIS 분봉 수집기")
    p.add_argument("--symbols", help="쉼표 구분 종목코드 (예: 005930,000660)")
    p.add_argument("--universe-file", help="종목코드 목록 파일 (한 줄에 하나)")
    p.add_argument("--from-screener", action="store_true",
                   help="거래량 랭킹 상위 종목 자동 선정")
    p.add_argument("--top", type=int, default=20, help="랭킹 사용 시 수집 종목 수")
    p.add_argument("--date", help="저장 날짜 지정 (YYYYMMDD, 기본=오늘)")
    p.add_argument("--overwrite", action="store_true", help="기존 파일 덮어쓰기")
    p.add_argument("--status", action="store_true", help="수집 현황만 출력")
    p.add_argument("--dir", default="data/candles", help="저장 디렉토리")
    args = p.parse_args()

    os.chdir(Path(__file__).parent.parent)
    load_dotenv()
    setup_logger(level="INFO")

    if args.status:
        CandleCollector(None, args.dir).print_coverage()
        return

    today = datetime.now(tz=KST)
    if not is_trading_day(today.date()):
        logger.warning("오늘(%s)은 휴장일입니다 — 수집할 데이터가 없습니다",
                       today.strftime("%Y-%m-%d"))
        return

    if today.hour < 15 or (today.hour == 15 and today.minute < 30):
        logger.warning(
            "장 마감(15:30) 전입니다 — 현재 %s. 부분 데이터만 수집됩니다.",
            today.strftime("%H:%M"),
        )

    client = KISClient(KISAuth())
    collector = CandleCollector(client, args.dir)

    # 대상 종목 결정
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.universe_file:
        with open(args.universe_file, encoding="utf-8") as f:
            symbols = [
                line.strip().split(",")[0] for line in f
                if line.strip() and not line.startswith("#")
            ]
    elif args.from_screener:
        rows = client.get_volume_rank()
        symbols = [
            str(r["mksc_shrn_iscd"]) for r in rows if r.get("mksc_shrn_iscd")
        ][:args.top]
        logger.info("거래량 랭킹 상위 %d종목 선정", len(symbols))
    else:
        symbols = _default_watchlist()

    collector.collect(symbols, args.date, args.overwrite)
    collector.print_coverage()


if __name__ == "__main__":
    main()
