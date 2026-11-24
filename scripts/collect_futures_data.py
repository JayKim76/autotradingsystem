"""
collect_futures_data.py — KOSPI200 선물 연속 시리즈 제작 도구 (Phase 2)

현실 (2026-11-24 확인): pykrx의 선물 기간 조회는 미구현이라, 장기 일별
연속물을 API로 한 번에 받을 수 없다. 이 도구는 두 가지를 제공한다.

1) 스티칭 (--stitch <폴더>):
   KRX 포털 등에서 받은 "계약별" 일별 CSV들을 최근월물 연속 시리즈로 잇는다.
   입력 폴더의 파일명 규칙: <만기YYYYMM>.csv (예: 199609.csv), 컬럼은
   date,open,high,low,close,volume (기존 일봉 포맷).
   방식: 각 날짜에 거래 가능한 계약 중 가장 가까운 만기를 선택하고,
   롤오버 시점의 가격 비율로 과거 구간 전체를 조정한다 (비율 조정 연속물).
   출력: data/futures_daily/KOSPI200F.csv

2) pykrx 스냅샷 수집 (--snapshot-days N):
   오늘부터 N일간의 일자별 전 계약 스냅샷을 받아 최근월물만 골라 저장한다.
   paper 전진 검증용 최근 데이터 수집에만 쓴다 (장기 수집에는 부적합).

사용법:
    python scripts/collect_futures_data.py --stitch data/futures_contracts
    python scripts/collect_futures_data.py --snapshot-days 30
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.resolve()))


def stitch_continuous(contracts: dict[str, list[dict]]) -> list[dict]:
    """{만기YYYYMM: [일봉 dict]} → 비율 조정된 최근월물 연속 시리즈.

    각 날짜의 최근월물 = 그 날짜에 데이터가 있는 가장 이른 만기.
    롤오버 시 겹치는 날짜의 (새 계약 종가 / 구 계약 종가) 비율을 누적
    조정계수에 곱해 가격 점프를 제거한다 (수익률 연속성 유지가 목적).
    """
    lookup: dict[str, dict[str, dict]] = {}
    for exp, bars in contracts.items():
        for b in bars:
            lookup.setdefault(b["date"], {})[exp] = b
    all_dates = sorted(lookup)
    expiries = sorted(contracts)

    out: list[dict] = []
    adj = 1.0
    prev_exp: str | None = None
    prev_date: str | None = None
    for date in all_dates:
        exp = next(e for e in expiries if e in lookup[date])
        bar = lookup[date][exp]
        if prev_exp is not None and exp != prev_exp:
            # 겹치는 날짜(롤 당일 또는 전일)에서 조정 비율 산출
            factor = None
            for cand in (date, prev_date):
                if cand and prev_exp in lookup.get(cand, {}) and exp in lookup.get(cand, {}):
                    old_c = lookup[cand][prev_exp]["close"]
                    new_c = lookup[cand][exp]["close"]
                    if old_c > 0:
                        factor = new_c / old_c
                        break
            if factor:
                adj *= factor
        out.append({
            "date": date,
            "open": bar["open"] / adj, "high": bar["high"] / adj,
            "low": bar["low"] / adj, "close": bar["close"] / adj,
            "volume": bar.get("volume", 0),
        })
        prev_exp, prev_date = exp, date
    return out


_KR_HEADERS = {"일자": "date", "시가": "open", "고가": "high", "저가": "low",
               "종가": "close", "거래량": "volume"}


def _num(value) -> float:
    return float(str(value).replace(",", "").replace('"', "").strip())


def _parse_bar(row: dict) -> dict | None:
    """영문/한글(KRX 포털) 헤더 모두 지원.

    KRX 포털 특이사항: 시장구분 '전체'로 받으면 날짜 셀에 (주간)/(야간)이 붙고
    같은 날짜가 두 번 나온다. 야간 행은 버리고 정규(주간) 행만 쓴다.
    날짜는 셀에서 YYYYMMDD 8자리를 추출한다.
    """
    import re
    norm = {}
    for k, v in row.items():
        if k is None:
            continue
        key = _KR_HEADERS.get(k.strip(), k.strip().lower())
        norm[key] = v
    raw_date = str(norm.get("date") or "")
    if "야간" in raw_date:
        return None
    m = re.search(r"(19|20)\d{6}", raw_date.replace("/", "").replace("-", ""))
    if not m:
        return None
    try:
        return {
            "date": m.group(0),
            "open": _num(norm["open"]), "high": _num(norm["high"]),
            "low": _num(norm["low"]), "close": _num(norm["close"]),
            "volume": int(_num(norm.get("volume") or 0)),
        }
    except (KeyError, ValueError):
        return None


def _expiry_from_filename(stem: str) -> str | None:
    """파일명에서 만기 YYYYMM 추출 (예: 201212, 코스피200선물_201212)."""
    import re
    for m in re.finditer(r"(19|20)\d{4}", stem):
        cand = m.group(0)
        if 1 <= int(cand[4:6]) <= 12:
            return cand
    return None


def _load_contract_dir(folder: Path) -> dict[str, list[dict]]:
    contracts: dict[str, list[dict]] = {}
    for path in sorted(folder.glob("*.csv")):
        expiry = _expiry_from_filename(path.stem) or path.stem
        bars = []
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                bar = _parse_bar(row)
                if bar:
                    bars.append(bar)
        if bars:
            # 같은 만기 파일이 여러 개면(기간 나눠 받기) 날짜 기준 병합
            merged = {b["date"]: b for b in contracts.get(expiry, [])}
            merged.update({b["date"]: b for b in bars})
            contracts[expiry] = sorted(merged.values(), key=lambda b: b["date"])
    return contracts


def main() -> int:
    p = argparse.ArgumentParser(description="KOSPI200 선물 연속 시리즈 제작")
    p.add_argument("--stitch", help="계약별 CSV 폴더 (파일명=만기YYYYMM.csv)")
    p.add_argument("--snapshot-days", type=int, default=0,
                   help="pykrx 스냅샷으로 최근 N일 최근월물 수집 (paper용)")
    p.add_argument("--out-dir", default="data/futures_daily")
    p.add_argument("--symbol", default="KOSPI200F")
    args = p.parse_args()

    from src.config_loader import resolve_repo_path
    from src.swing import DailyBar, save_daily_csv

    out_dir = Path(resolve_repo_path(args.out_dir))

    if args.stitch:
        folder = Path(resolve_repo_path(args.stitch))
        contracts = _load_contract_dir(folder)
        if not contracts:
            print(f"계약 파일이 없습니다: {folder}")
            return 1
        series = stitch_continuous(contracts)
        bars = [DailyBar(date=r["date"], open=r["open"], high=r["high"],
                         low=r["low"], close=r["close"], volume=r["volume"])
                for r in series]
        path = save_daily_csv(out_dir, args.symbol, bars)
        print(f"스티칭 완료: 계약 {len(contracts)}개 → {len(bars)}행 "
              f"{bars[0].date}~{bars[-1].date} → {path}")
        return 0

    if args.snapshot_days > 0:
        print("pykrx 스냅샷 수집은 KRX 로그인이 필요하고 일자별 호출이라 "
              "최근 구간 전용입니다. 기간 조회 미구현으로 장기 수집은 불가합니다.")
        print("장기 데이터는 KRX 포털에서 계약별 CSV를 받아 --stitch를 쓰세요.")
        return 2

    print("--stitch <폴더> 또는 --snapshot-days N 중 하나를 지정하세요.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
