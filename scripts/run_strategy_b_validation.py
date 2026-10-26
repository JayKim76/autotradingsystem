"""
run_strategy_b_validation.py — 전략 B(20일선 눌림목) 홀드아웃 검증 CLI

사용법 (수집 데이터는 최종 규칙·전략 A와 공유한다):
    python scripts/run_strategy_b_validation.py

규칙은 전략B설계.md에 동결돼 있다. 주의: 홀드아웃은 이미 최종 규칙·전략 A
검증에 사용된 구간으로, 이 검증은 사전 약정 변경 하에 수행되는 세 번째
조회다 (설계 문서 §0). 결과는 data/validation/ 아래에
strategy-b-report-*.md/.json으로 저장된다. 판정: PASS(0)/FAIL(1)/INSUFFICIENT(2)
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.resolve()))

from src.config_loader import load_config, resolve_repo_path  # noqa: E402
from src.final_rules import filter_bars_until  # noqa: E402
from src.strategy_b import StrategyBBacktester, StrategyBConfig  # noqa: E402
from src.swing import load_daily_dir, render_swing_report, swing_walk_forward  # noqa: E402
from src.validation import verdict  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="전략 B 홀드아웃 검증 (약정 변경 — 세 번째 조회)")
    p.add_argument("--data-dir", default="data/daily_holdout")
    p.add_argument("--end", default="20231023", help="검증 종료일 YYYYMMDD (이후 데이터 절단)")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--strategy-config", default="config_strategy_b.yaml")
    p.add_argument("--capital", type=float, default=10_000_000)
    p.add_argument("--folds", type=int, default=4)
    p.add_argument("--out-dir", default="data/validation")
    args = p.parse_args()

    data_dir = str(resolve_repo_path(args.data_dir))
    raw = load_daily_dir(data_dir)
    bars_by_symbol = filter_bars_until(raw, args.end)
    n_symbols = len(bars_by_symbol)
    all_dates = sorted({b.date for bars in bars_by_symbol.values() for b in bars})
    if all_dates:
        print(f"데이터: {len(all_dates)}거래일, 종목 {n_symbols}개 "
              f"({all_dates[0]} ~ {all_dates[-1]}, {data_dir})")
    else:
        print(f"데이터: 0거래일 ({data_dir}) — 먼저 홀드아웃 수집을 하세요: "
              "python scripts/collect_daily.py --universe-file universe_kospi200.csv "
              "--years 13 --dir data/daily_holdout")
        return 2

    base_config = load_config(args.config)
    strategy_dict = load_config(args.strategy_config) if Path(args.strategy_config).exists() else {}
    cfg = StrategyBConfig.from_config(strategy_dict)
    regime_active = cfg.regime_symbol in bars_by_symbol

    result = swing_walk_forward(
        bars_by_symbol, cfg, base_config,
        capital=args.capital, n_folds=args.folds,
        backtester_cls=StrategyBBacktester,
    )
    v = verdict(result["oos_metrics"])
    report = render_swing_report(result, v, data_dir=f"{data_dir} (홀드아웃 ~{args.end}, 세 번째 조회)",
                                 regime_active=regime_active, capital=args.capital,
                                 title="전략 B 검증 리포트 (20일선 눌림목, 홀드아웃 재사용)")
    print(report)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"strategy-b-report-{stamp}.md").write_text(report, encoding="utf-8")
    payload = {
        "generated_at": stamp,
        "data_dir": data_dir,
        "holdout_end": args.end,
        "holdout_reuse": "3rd look (final rules, strategy A, strategy B) — pre-agreement amended by user 2026-10-27",
        "verdict": v,
        "oos_metrics": result["oos_metrics"],
        "folds": [
            {"fold": f["fold"], "start": f["start"], "end": f["end"],
             "metrics": f["metrics"], "end_capital": f["end_capital"]}
            for f in result["folds"]
        ],
    }
    (out_dir / f"strategy-b-report-{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"저장: {out_dir}/strategy-b-report-{stamp}.md / .json")

    return {"PASS": 0, "FAIL": 1}.get(v["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
