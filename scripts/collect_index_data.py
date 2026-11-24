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
    """FRED GOLDAMGBD228NLBM (LBMA 금 AM fix, USD/oz) — 금 1990년대 보완용.

    FRED가 연결을 끊는 경우가 있어 3회 재시도한다. 그래도 실패하면
    브라우저로 받은 CSV를 --import-csv 로 넣는 경로를 쓴다.
    """
    import requests
    url = ("https://fred.stlouisfed.org/graph/fredgraph.csv"
           f"?id=GOLDAMGBD228NLBM&cosd={START[:4]}-{START[4:6]}-{START[6:]}"
           f"&coed={END[:4]}-{END[4:6]}-{END[6:]}")
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=60, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "text/csv,*/*",
            })
            r.raise_for_status()
            rows = parse_fred_csv(r.text)
            path, n = save_series(out_dir, "132030", rows)
            span = f"{rows[0][0]}~{rows[-1][0]}" if rows else "-"
            return f"132030 (FRED LBMA 금): {n}행 {span} → {path}"
        except Exception as e:  # noqa: BLE001 - 재시도 목적
            last_err = e
            time.sleep(5)
    raise RuntimeError(f"FRED 금 수집 3회 실패 ({last_err}) — "
                       "브라우저로 Stooq XAUUSD CSV를 받아 --import-csv를 쓰세요")


def collect_fred_gold_api(out_dir: Path, api_key: str) -> str:
    """FRED 정식 API (api.stlouisfed.org) — fredgraph가 막힐 때의 경로.

    무료 API 키: fred.stlouisfed.org 회원가입 → My Account → API Keys.
    키는 환경변수 FRED_API_KEY로만 받는다 (파일·깃에 저장 금지).
    """
    import requests
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": "GOLDAMGBD228NLBM",
        "api_key": api_key,
        "file_type": "json",
        "observation_start": f"{START[:4]}-{START[4:6]}-{START[6:]}",
        "observation_end": f"{END[:4]}-{END[4:6]}-{END[6:]}",
        "limit": 100000,
    }
    r = requests.get(url, params=params, timeout=60,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    rows = parse_fred_observations(r.json())
    path, n = save_series(out_dir, "132030", rows)
    span = f"{rows[0][0]}~{rows[-1][0]}" if rows else "-"
    return f"132030 (FRED API LBMA 금): {n}행 {span} → {path}"


def parse_fred_observations(payload: dict) -> list[tuple[str, float, float, float, float]]:
    """FRED API observations JSON → 로더 행. 결측(.)은 건너뜀."""
    rows = []
    for obs in payload.get("observations", []):
        try:
            v = float(obs["value"])
        except (KeyError, ValueError):
            continue
        d = str(obs.get("date", "")).replace("-", "")
        if len(d) == 8:
            rows.append((d, v, v, v, v))
    return sorted(rows)


def parse_fred_csv(text: str) -> list[tuple[str, float, float, float, float]]:
    """fredgraph CSV (DATE,VALUE) → 로더 행. 결측(.)은 건너뜀."""
    rows = []
    for line in text.strip().splitlines()[1:]:
        date_str, _, val = line.partition(",")
        try:
            v = float(val)
        except ValueError:
            continue
        rows.append((date_str.replace("-", ""), v, v, v, v))
    return sorted(rows)


def parse_generic_csv(text: str) -> list[tuple[str, float, float, float, float]]:
    """Date,Open,High,Low,Close 형식 CSV (Stooq 등) → 로더 행."""
    import io
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            d = (row.get("Date") or row.get("date") or "").replace("-", "")
            o = float(row.get("Open") or row.get("open"))
            h = float(row.get("High") or row.get("high"))
            l = float(row.get("Low") or row.get("low"))
            c = float(row.get("Close") or row.get("close"))
        except (TypeError, ValueError):
            continue
        if len(d) == 8:
            rows.append((d, o, h, l, c))
    return sorted(rows)


def merge_gold_series(early: list, late: list) -> list:
    """금 병합: late의 첫 날짜 전날까지는 early(LBMA 현물), 이후는 late(선물).

    둘 다 USD/oz 금 가격이라 수준이 호환된다 (선물 베이시스 차이는 감수,
    지수데이터출처.md에 기록).
    """
    if not late:
        return sorted(early)
    cutoff = late[0][0]
    merged = [r for r in early if r[0] < cutoff] + list(late)
    return sorted(merged)


def main() -> int:
    p = argparse.ArgumentParser(description="기초지수 대체 데이터 수집 (1990~2012)")
    p.add_argument("--only", help="쉼표 구분 ETF 코드 (기본: Yahoo 소스 전부)")
    p.add_argument("--out-dir", default="data/index_daily")
    p.add_argument("--gold-source", choices=["yahoo", "fred"], default="yahoo",
                   help="금(132030) 소스: yahoo=GC=F(2000~), fred=LBMA(1990~)")
    p.add_argument("--import-csv", help="브라우저로 받은 CSV 파일 경로 (Stooq 등)")
    p.add_argument("--import-symbol", default="132030",
                   help="임포트할 ETF 코드 (기본 132030 금)")
    p.add_argument("--merge-gold", action="store_true",
                   help="data/index_daily_fred/132030.csv(현물)와 data/index_daily/132030.csv(선물)를 병합")
    args = p.parse_args()

    from src.config_loader import resolve_repo_path
    out_dir = Path(resolve_repo_path(args.out_dir))

    if args.import_csv:
        text = Path(args.import_csv).read_text(encoding="utf-8-sig")
        rows = parse_generic_csv(text)
        if not rows:  # FRED 형식일 수도 있다
            rows = parse_fred_csv(text)
        rows = [r for r in rows if START <= r[0] <= END]
        path, n = save_series(out_dir, args.import_symbol, rows)
        span = f"{rows[0][0]}~{rows[-1][0]}" if rows else "-"
        print(f"{args.import_symbol} (CSV 임포트): {n}행 {span} → {path}")
        return 0 if rows else 1

    if args.merge_gold:
        main_dir = Path(resolve_repo_path("data/index_daily"))
        early_path = Path(resolve_repo_path("data/index_daily_fred")) / "132030.csv"
        late_path = main_dir / "132030.csv"
        if not early_path.exists() or not late_path.exists():
            print(f"병합 불가: {early_path} 또는 {late_path}가 없습니다")
            return 1

        def _load(path):
            with open(path, encoding="utf-8-sig") as f:
                return sorted(
                    (r["date"].replace("-", ""), float(r["open"]), float(r["high"]),
                     float(r["low"]), float(r["close"]))
                    for r in csv.DictReader(f))

        merged = merge_gold_series(_load(early_path), _load(late_path))
        path, n = save_series(main_dir, "132030", merged)
        print(f"132030 (금 병합): {n}행 {merged[0][0]}~{merged[-1][0]} → {path}")
        return 0

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
                api_key = os.getenv("FRED_API_KEY", "")
                if api_key:
                    print(collect_fred_gold_api(out_dir, api_key))
                else:
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
