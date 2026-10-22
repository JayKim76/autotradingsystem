"""
collect_daily.py — KIS 일봉 장기 수집기 (스윙 전략 검증용)

사용법:
    python scripts/collect_daily.py                       # 기본 유니버스 + 069500, 최근 3년
    python scripts/collect_daily.py --universe-file universe_kospi200.csv   # KOSPI 200 전체
    python scripts/collect_daily.py --symbols 005930,000660 --years 5
    python scripts/collect_daily.py --status              # 수집 현황만

KIS 기간별 시세(FHKST03010100)는 한 번에 최대 100행만 반환하므로
종료일을 거꾸로 이동하며 페이지네이션한다. 이미 받은 구간은 건너뛴다.
저장: data/daily/<종목>.csv (date,open,high,low,close,volume) — 수정주가 기준.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.resolve()))

REGIME_SYMBOL = "069500"   # KODEX 200 — 레짐 판정용으로 항상 함께 수집


def _rows_to_bars(rows: list[dict]):
    from src.swing import DailyBar
    bars = []
    for r in rows:
        try:
            bars.append(DailyBar(
                date=str(r["stck_bsop_date"]),
                open=float(r["stck_oprc"]), high=float(r["stck_hgpr"]),
                low=float(r["stck_lwpr"]), close=float(r["stck_clpr"]),
                volume=int(float(r.get("acml_vol") or 0)),
            ))
        except (KeyError, ValueError):
            continue
    return bars


def fetch_history(client, symbol: str, start: str, end: str):
    """start~end 구간의 일봉 전부를 페이지네이션으로 수집 (날짜 오름차순)"""
    collected: dict[str, object] = {}
    cursor = end
    while cursor >= start:
        rows = client.get_daily_candles(symbol, period="D", start=start, end=cursor)
        bars = _rows_to_bars(rows or [])
        if not bars:
            break
        for b in bars:
            if start <= b.date <= end:
                collected[b.date] = b
        earliest = min(b.date for b in bars)
        if earliest <= start or len(bars) < 100:
            break
        cursor = (datetime.strptime(earliest, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    return [collected[d] for d in sorted(collected)]


def main() -> int:
    p = argparse.ArgumentParser(description="KIS 일봉 장기 수집기")
    p.add_argument("--symbols", help="쉼표 구분 종목코드 (기본: 스크리너 유니버스)")
    p.add_argument("--universe-file", help="종목코드 목록 파일 (CSV/텍스트, 예: universe_kospi200.csv)")
    p.add_argument("--years", type=int, default=3, help="수집 기간 (년)")
    p.add_argument("--dir", default="data/daily", help="저장 디렉토리")
    p.add_argument("--status", action="store_true", help="수집 현황만 출력")
    args = p.parse_args()

    os.chdir(Path(__file__).parent.parent)

    from src.swing import load_daily_dir, save_daily_csv

    if args.status:
        data = load_daily_dir(args.dir)
        if not data:
            print(f"수집된 일봉이 없습니다 ({args.dir})")
            return 0
        for symbol, bars in sorted(data.items()):
            print(f"{symbol}: {len(bars)}일 ({bars[0].date} ~ {bars[-1].date})")
        return 0

    from dotenv import load_dotenv
    load_dotenv()
    from src.auth import KISAuth
    from src.kis_client import KISClient

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.universe_file:
        from src.collector import load_universe_file
        symbols = load_universe_file(args.universe_file)
        print(f"유니버스 파일에서 {len(symbols)}종목 로드: {args.universe_file}")
    else:
        from src.screener import default_universe
        symbols = default_universe()
    if REGIME_SYMBOL not in symbols:
        symbols.append(REGIME_SYMBOL)

    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=int(args.years * 365.25))).strftime("%Y%m%d")

    client = KISClient(KISAuth())
    existing = load_daily_dir(args.dir)
    for symbol in symbols:
        bars = fetch_history(client, symbol, start, end)
        if not bars:
            print(f"{symbol}: 데이터 없음")
            continue
        merged = {b.date: b for b in existing.get(symbol, [])}
        for b in bars:
            merged[b.date] = b
        save_daily_csv(args.dir, symbol, list(merged.values()))
        print(f"{symbol}: {len(merged)}일 ({min(merged)} ~ {max(merged)}) 저장")
    return 0


if __name__ == "__main__":
    sys.exit(main())
