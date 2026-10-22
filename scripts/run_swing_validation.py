"""
run_swing_validation.py — 일봉 스윙 전략 워크포워드 검증 CLI

사용법:
    python scripts/run_swing_validation.py                    # data/daily 기준
    python scripts/run_swing_validation.py --folds 5
    python scripts/run_swing_validation.py --from-candles     # 분봉 수집본을 일봉화해 사용

데이터가 없으면 먼저 수집한다 (KIS 키 필요):
    python scripts/collect_daily.py
결과는 data/validation/ 아래에 swing-report-*.md/.json으로 저장된다.
판정: PASS(0) / FAIL(1) / INSUFFICIENT·데이터부족(2)
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.resolve()))

from src.config_loader import load_config, resolve_repo_path  # noqa: E402
from src.swing import (SwingConfig, aggregate_daily, load_daily_dir,  # noqa: E402
                       render_swing_report, swing_walk_forward)
from src.validation import verdict  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="일봉 스윙 전략 워크포워드 검증")
    p.add_argument("--data-dir", default="data/daily")
    p.add_argument("--from-candles", action="store_true",
                   help="data/daily 대신 분봉 수집본(data/candles)을 일봉화해 사용")
    p.add_argument("--candles-dir", default="data/candles")
    p.add_argument("--config", default="config.yaml", help="비용 모델용 기본 설정")
    p.add_argument("--swing-config", default="config_swing.yaml")
    p.add_argument("--capital", type=float, default=10_000_000)
    p.add_argument("--folds", type=int, default=4)
    p.add_argument("--min-trades", type=int, default=None,
                   help="OOS 최소 거래 수 게이트 (기본 30)")
    p.add_argument("--out-dir", default="data/validation")
    args = p.parse_args()

    if args.from_candles:
        from src.collector import load_range
        candles_dir = str(resolve_repo_path(args.candles_dir))
        minute_data = load_range(candles_dir)
        bars_by_symbol = aggregate_daily(minute_data)
        data_label = f"{candles_dir} (분봉 집계)"
    else:
        data_dir = str(resolve_repo_path(args.data_dir))
        bars_by_symbol = load_daily_dir(data_dir)
        data_label = data_dir

    n_symbols = len(bars_by_symbol)
    n_days = len({b.date for bars in bars_by_symbol.values() for b in bars})
    print(f"데이터: {n_days}거래일, 종목 {n_symbols}개 ({data_label})")
    if n_symbols == 0:
        print("판정 불가: 일봉 데이터가 없습니다. 먼저 수집하세요: "
              "python scripts/collect_daily.py")
        return 2

    base_config = load_config(args.config)
    swing_config_dict = load_config(args.swing_config) if Path(args.swing_config).exists() else {}
    swing = SwingConfig.from_config(swing_config_dict)
    regime_active = swing.regime_symbol in bars_by_symbol

    result = swing_walk_forward(
        bars_by_symbol, swing, base_config,
        capital=args.capital, n_folds=args.folds,
    )
    gates = {}
    if args.min_trades is not None:
        gates["min_oos_trades"] = args.min_trades
    v = verdict(result["oos_metrics"], gates or None)
    report = render_swing_report(result, v, data_dir=data_label,
                                 regime_active=regime_active, capital=args.capital)
    print(report)

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute() and str(resolve_repo_path(args.data_dir)) != args.data_dir:
        from src.config_loader import REPO_ROOT
        out_dir = REPO_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"swing-report-{stamp}.md").write_text(report, encoding="utf-8")
    payload = {
        "generated_at": stamp,
        "data_dir": data_label,
        "verdict": v,
        "oos_metrics": {k: val for k, val in result["oos_metrics"].items()},
        "folds": [
            {"fold": f["fold"], "start": f["start"], "end": f["end"],
             "metrics": f["metrics"], "end_capital": f["end_capital"]}
            for f in result["folds"]
        ],
    }
    (out_dir / f"swing-report-{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"저장: {out_dir}/swing-report-{stamp}.md / .json")

    return {"PASS": 0, "FAIL": 1}.get(v["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
