"""
run_walkforward.py — 실데이터 워크포워드 검증 CLI

사용법:
    python scripts/run_walkforward.py                       # 기준 설정, 4폴드
    python scripts/run_walkforward.py --optimize            # 폴드별 파라미터 선택 포함
    python scripts/run_walkforward.py --folds 5 --from 20260101 --to 20260630

데이터가 없으면 먼저 수집한다 (KIS 키 필요):
    python scripts/collect_candles.py
결과는 data/validation/ 아래에 Markdown + JSON으로 저장된다.
판정: PASS(0) / FAIL(1) / INSUFFICIENT·데이터부족(2) 을 종료코드로도 구분한다.
"""
import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.resolve()))

from src.collector import load_range
from src.config_loader import load_config
from src.validation import render_report, verdict, walk_forward


def main() -> int:
    p = argparse.ArgumentParser(description="실데이터 워크포워드 백테스트 검증")
    p.add_argument("--data-dir", default="data/candles")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--config-override", action="append", default=None)
    p.add_argument("--capital", type=float, default=10_000_000)
    p.add_argument("--folds", type=int, default=4)
    p.add_argument("--optimize", action="store_true",
                   help="각 폴드의 파라미터를 이전 구간에서만 골라 적용 (진짜 워크포워드)")
    p.add_argument("--from", dest="start", default=None, help="시작일 YYYYMMDD")
    p.add_argument("--to", dest="end", default=None, help="종료일 YYYYMMDD")
    p.add_argument("--min-days", type=int, default=20,
                   help="최소 거래일 수 (미만이면 판정하지 않음)")
    p.add_argument("--out-dir", default="data/validation")
    args = p.parse_args()

    data = load_range(args.data_dir, start=args.start, end=args.end)
    n_days = len(data)
    n_symbols = len({s for day in data.values() for s in day})
    print(f"데이터: {n_days}거래일, 종목 {n_symbols}개 ({args.data_dir})")

    if n_days < args.min_days:
        print(f"판정 불가: 최소 {args.min_days}거래일이 필요한데 {n_days}일뿐입니다. "
              f"먼저 데이터를 수집하세요: python scripts/collect_candles.py")
        return 2

    config = load_config(args.config, args.config_override)
    result = walk_forward(
        data, config, capital=args.capital,
        n_folds=args.folds, optimize=args.optimize,
    )
    v = verdict(result["oos_metrics"])
    report = render_report(result, v, data_dir=args.data_dir)
    print(report)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"report-{stamp}.md").write_text(report, encoding="utf-8")
    payload = {
        "generated_at": stamp,
        "data_dir": args.data_dir,
        "verdict": v,
        "oos_metrics": result["oos_metrics"],
        "folds": [
            {**asdict(f), "metrics": f.metrics} for f in result["folds"]
        ],
    }
    (out_dir / f"report-{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )
    print(f"저장: {out_dir}/report-{stamp}.md / .json")

    return {"PASS": 0, "FAIL": 1}.get(v["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
