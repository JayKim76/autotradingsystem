"""
collect_index_data.py — ETF 듀얼모멘텀 검증용 기초지수 대체 데이터 수집기

사전등록-ETF듀얼모멘텀.md의 1차 검증 데이터(1990~2012)를 모은다.
저장: data/index_daily/<ETF코드>.csv (date,open,high,low,close,volume)
     — 엔진이 ETF 코드 그대로 읽을 수 있게 지수를 ETF 코드 파일명으로 저장.

소스 매핑 (2026-11-24 실측 기준, 지수데이터출처.md 참조):
  069500 KODEX 200        ← KRX KOSPI200 지수 (pykrx, KRX 회원 로그인 필요, PC 전용)
  360750 TIGER S&P500     ← Yahoo ^GSPC
  379800 KODEX 나스닥100  ← Yahoo ^NDX
  182490 TIGER CSI300     ← Eastmoney CSI300 지수 (2005~ — 지수 자체가 2005 출시)
  132030 KODEX 골드선물   ← Yahoo GC=F (2000-08~). 1990년대는 FRED GOLDAMGBD228NLBM (LBMA 금)
  153130 KODEX 단기채권   ← Yahoo ^IRX (미 13주 T-bill 금리) → 가격 시리즈로 변환

T-bill 변환: 금리는 연율 % — 전일 종가 × (1 + 금리/100 × 경과일수/360) 로 누적.
(단기채권의 일별 이자 누적을 근사한다. 절대모멘텀 비교 기준이 목적이라
수준(level)보다 수익률 경로가 중요하다.)

사용법:
    python scripts/collect_index_data.py                  # Yahoo 소스 전부
    python scripts/collect_index_data.py --only 360750,379800
    python scripts/collect_index_data.py --only 069500    # KRX (PC + KRX_ID/KRX_PW 환경변수)
    python scripts/collect_index_data.py --gold-source fred   # 금 1990년대 FRED로 시도

주의: 수집은 데이터 "확보"일 뿐 검증 "조회"가 아니다. 검증 실행은 단 1회만.
"""
import argparse
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.resolve()))

START = "19900101"
END = "20121231"

YAHOO_MAP = {
    "360750": "^GSPC",
    "379800": "^NDX",
    "182490": "000300.SS",
    "132030": "GC=F",
    "153130": "^IRX",
}


def yield_to_price_series(rows: list[tuple[str, float]],
                          start_price: float = 100.0) -> list[tuple[str, float]]:
    """(날짜, 연율%) → (날짜, 가격). 경과일수/360 일할 누적."""
    out: list[tuple[str, float]] = []
    price = start_price
    prev_date: datetime | None = None
    prev_yield = 0.0
    for date_str, y in rows:
        d = datetime.strptime(date_str, "%Y%m%d")
        if prev_date is not None:
            days = (d - prev_date).days
            price *= (1.0 + prev_yield / 100.0 * days / 360.0)
        out.append((date_str, price))
        prev_date, prev_yield = d, y
    return out


def save_series(out_dir: Path, symbol: str, rows: list[tuple[str, float, float, float, float]]):
    """rows: (date, open, high, low, close)"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{symbol}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for date_str, o, h, l, c in rows:
            w.writerow([date_str, f"{o:.4f}", f"{h:.4f}", f"{l:.4f}", f"{c:.4f}", 0])
    return path, len(rows)


def fetch_yahoo(ticker: str, retries: int = 3):
    import yfinance as yf
    from datetime import timedelta
    start = f"{START[:4]}-{START[4:6]}-{START[6:]}"
    end_dt = datetime.strptime(END, "%Y%m%d") + timedelta(days=1)  # end exclusive
    end = end_dt.strftime("%Y-%m-%d")
    last_err = None
    for attempt in range(retries):
        try:
            h = yf.download(ticker, start=start, end=end, progress=False,
                            threads=False, timeout=30, auto_adjust=False)
            if len(h):
                return h
            last_err = "empty"
        except Exception as e:  # noqa: BLE001 - 재시도 목적
            last_err = f"{type(e).__name__}: {str(e)[:80]}"
        time.sleep(5)
    raise RuntimeError(f"Yahoo {ticker} 수집 실패 ({last_err})")


def rows_from_yahoo(h) -> list[tuple[str, float, float, float, float]]:
    rows = []
    for ts, r in h.iterrows():
        try:
            rows.append((ts.strftime("%Y%m%d"), float(r["Open"]), float(r["High"]),
                         float(r["Low"]), float(r["Close"])))
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(rows)


def collect_yahoo_symbol(symbol: str, out_dir: Path) -> str:
    ticker = YAHOO_MAP[symbol]
    h = fetch_yahoo(ticker)
    if symbol == "153130":
        # ^IRX는 금리 — 종가(금리)를 가격 시리즈로 변환
        closes = [(ts.strftime("%Y%m%d"), float(r["Close"])) for ts, r in h.iterrows()]
        closes.sort()
        prices = yield_to_price_series(closes)
        rows = [(d, p, p, p, p) for d, p in prices]
    else:
        rows = rows_from_yahoo(h)
    path, n = save_series(out_dir, symbol, rows)
    span = f"{rows[0][0]}~{rows[-1][0]}" if rows else "-"
    return f"{symbol} ({ticker}): {n}행 {span} → {path}"


def collect_eastmoney_csi300(out_dir: Path) -> str:
    """CSI300 지수 — Eastmoney 공개 API (secid 1.000300). 지수 출시 2005년부터."""
    import requests
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           "?secid=1.000300&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56"
           "&klt=101&fqt=0&beg=20050101&end=20121231&lmt=100000")
    r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    klines = (r.json().get("data") or {}).get("klines") or []
    rows = []
    for line in klines:
        parts = line.split(",")
        # f51 날짜, f52 시가, f53 종가, f54 고가, f55 저가
        rows.append((parts[0].replace("-", ""), float(parts[1]), float(parts[3]),
                     float(parts[4]), float(parts[2])))
    rows.sort()
    path, n = save_series(out_dir, "182490", rows)
    span = f"{rows[0][0]}~{rows[-1][0]}" if rows else "-"
    return f"182490 (Eastmoney CSI300): {n}행 {span} → {path}"


def collect_krx_kospi200(out_dir: Path) -> str:
    """KRX KOSPI200 (지수코드 1028). KRX_ID/KRX_PW 환경변수 필요, PC 전용."""
    if not (os.getenv("KRX_ID") and os.getenv("KRX_PW")):
        raise RuntimeError("KRX_ID/KRX_PW 환경변수가 없습니다. "
                           "data.krx.co.kr 무료 회원가입 후 설정하세요 (PC에서 실행).")
    from pykrx import stock
    df = stock.get_index_ohlcv_by_date(START, END, "1028")
    rows = []
    for ts, r in df.iterrows():
        rows.append((ts.strftime("%Y%m%d"), float(r["시가"]), float(r["고가"]),
                     float(r["저가"]), float(r["종가"])))
    rows.sort()
    path, n = save_series(out_dir, "069500", rows)
    span = f"{rows[0][0]}~{rows[-1][0]}" if rows else "-"
    return f"069500 (KRX KOSPI200): {n}행 {span} → {path}"


def collect_fred_gold(out_dir: Path) -> str:
    """FRED GOLDAMGBD228NLBM (LBMA 금 AM fix, USD/oz) — 금 1990년대 보완용."""
    import requests
    url = ("https://fred.stlouisfed.org/graph/fredgraph.csv"
           f"?id=GOLDAMGBD228NLBM&cosd={START[:4]}-{START[4:6]}-{START[6:]}"
           f"&coed={END[:4]}-{END[4:6]}-{END[6:]}")
    r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    rows = []
    for line in r.text.strip().splitlines()[1:]:
        date_str, _, val = line.partition(",")
        try:
            v = float(val)
        except ValueError:
            continue
        d = date_str.replace("-", "")
        rows.append((d, v, v, v, v))
    rows.sort()
    path, n = save_series(out_dir, "132030", rows)
    span = f"{rows[0][0]}~{rows[-1][0]}" if rows else "-"
    return f"132030 (FRED LBMA 금): {n}행 {span} → {path}"


def main() -> int:
    p = argparse.ArgumentParser(description="기초지수 대체 데이터 수집 (1990~2012)")
    p.add_argument("--only", help="쉼표 구분 ETF 코드 (기본: Yahoo 소스 전부)")
    p.add_argument("--out-dir", default="data/index_daily")
    p.add_argument("--gold-source", choices=["yahoo", "fred"], default="yahoo",
                   help="금(132030) 소스: yahoo=GC=F(2000~), fred=LBMA(1990~)")
    args = p.parse_args()

    from src.config_loader import resolve_repo_path
    out_dir = Path(resolve_repo_path(args.out_dir))

    targets = ([s.strip() for s in args.only.split(",")] if args.only
               else list(YAHOO_MAP.keys()))
    failures = 0
    for symbol in targets:
        try:
            if symbol == "069500":
                print(collect_krx_kospi200(out_dir))
            elif symbol == "182490":
                print(collect_eastmoney_csi300(out_dir))
            elif symbol == "132030" and args.gold_source == "fred":
                print(collect_fred_gold(out_dir))
            elif symbol in YAHOO_MAP:
                print(collect_yahoo_symbol(symbol, out_dir))
            else:
                print(f"{symbol}: 알 수 없는 코드 — 건너뜀")
                failures += 1
        except Exception as e:  # noqa: BLE001 - 종목별 실패 격리
            failures += 1
            print(f"{symbol}: 실패 — {e}")
    print(f"완료: 성공 {len(targets) - failures} / 실패 {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
