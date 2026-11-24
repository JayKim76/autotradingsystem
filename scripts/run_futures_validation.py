"""
run_futures_validation.py — KOSPI200 선물 TSMOM 워크포워드 검증 CLI (Phase 2)

사용법:
    python scripts/run_futures_validation.py                  # data/futures_daily 기준

주의 (사전등록-선물TSMOM.md):
  - 본 판정은 KOSPI200 선물 연속 시리즈 1996~2012로만 한다. 단 1회.
  - 소진된 지수 데이터(data/index_daily)로 대체 검증하지 않는다.
  - 결과는 data/validation/ 아래 futures-report-*.md/.json 으로 저장된다.
판정: PASS(0) / FAIL(1) / INSUFFICIENT·데이터부족(2)
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.resolve()))

from src.config_loader import load_config, resolve_repo_path  # noqa: E402
from src.futures_tsmom import FuturesTsmomBacktester, FuturesTsmomConfig  # noqa: E402
from src.swing import load_daily_dir, render_swing_report, swing_walk_forward  # noqa: E402
from src.validation import verdict  # noqa: E402

WARMUP_DAYS = 252


def main() -> int:
    p = argparse.ArgumentParser(description="KOSPI200 선물 TSMOM 워크포워드 검증")
    p.add_argument("--data-dir", default="data/futures_daily")
    p.add_argument("--config", default="config.yaml", help="기본 설정 (호환용)")
    p.add_argument("--futures-config", default="config_futures_tsmom.yaml")
    p.add_argument("--capital", type=float, default=10_000_000)
    p.add_argument("--folds", type=int, default=4)
    p.add_argument("--min-trades", type=int, default=None)
    p.add_argument("--out-dir", default="data/validation")
    args = p.parse_args()

    data_dir = str(resolve_repo_path(args.data_dir))
    bars_by_symbol = load_daily_dir(data_dir)
    n_symbols = len(bars_by_symbol)
    n_days = len({b.date for bars in bars_by_symbol.values() for b in bars})
    print(f"데이터: {n_days}거래일, 시리즈 {n_symbols}개 ({data_dir})")
    if n_symbols == 0:
        print("판정 불가: 선물 데이터가 없습니다. 먼저 확보하세요 "
              "(scripts/collect_futures_data.py 및 선물TSMOM설계.md 참조).")
        return 2

    base_config = load_config(args.config)
    fut_dict = load_config(args.futures_config) if Path(args.futures_config).exists() else {}
    cfg = FuturesTsmomConfig.from_config(fut_dict)

    result = swing_walk_forward(
        bars_by_symbol, cfg, base_config,
        capital=args.capital, n_folds=args.folds,
        warmup_days=WARMUP_DAYS,
        backtester_cls=FuturesTsmomBacktester,
    )
    gates = {}
    if args.min_trades is not None:
        gates["min_oos_trades"] = args.min_trades
    v = verdict(result["oos_metrics"], gates or None)
    report = render_swing_report(result, v, data_dir=data_dir,
                                 regime_active=False, capital=args.capital,
                                 title="KOSPI200 선물 TSMOM 검증 리포트 (Phase 2)")
    print(report)

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        from src.config_loader import REPO_ROOT
        out_dir = REPO_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"futures-report-{stamp}.md").write_text(report, encoding="utf-8")
    payload = {
        "generated_at": stamp,
        "data_dir": data_dir,
        "verdict": v,
        "oos_metrics": result["oos_metrics"],
        "folds": [
            {"fold": f["fold"], "start": f["start"], "end": f["end"],
             "metrics": f["metrics"], "end_capital": f["end_capital"]}
            for f in result["folds"]
        ],
    }
    (out_dir / f"futures-report-{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"저장: {out_dir}/futures-report-{stamp}.md / .json")

    return {"PASS": 0, "FAIL": 1}.get(v["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
