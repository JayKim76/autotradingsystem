"""
run_regime_breakout_validation.py — 레짐 조건부 돌파 전략 워크포워드 검증 CLI

사용법:
    python scripts/run_regime_breakout_validation.py          # data/daily 기준

데이터는 기존 수집본을 그대로 쓴다. 결과는 data/validation/ 아래에
regime-breakout-report-*.md/.json으로 저장된다.
판정: PASS(0) / FAIL(1) / INSUFFICIENT·데이터부족(2)
주의: 같은 데이터의 네 번째 설계다 (레짐돌파전략설계.md §0 고지 참조).
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.resolve()))

from src.config_loader import load_config, resolve_repo_path  # noqa: E402
from src.regime_breakout import RegimeBreakoutBacktester, RegimeBreakoutConfig  # noqa: E402
from src.swing import load_daily_dir, render_swing_report, swing_walk_forward  # noqa: E402
from src.validation import verdict  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="레짐 조건부 돌파 전략 워크포워드 검증")
    p.add_argument("--data-dir", default="data/daily")
    p.add_argument("--config", default="config.yaml", help="비용 모델용 기본 설정")
    p.add_argument("--strategy-config", default="config_regime_breakout.yaml")
    p.add_argument("--capital", type=float, default=10_000_000)
    p.add_argument("--folds", type=int, default=4)
    p.add_argument("--min-trades", type=int, default=None,
                   help="OOS 최소 거래 수 게이트 (기본 30)")
    p.add_argument("--out-dir", default="data/validation")
    args = p.parse_args()

    data_dir = str(resolve_repo_path(args.data_dir))
    bars_by_symbol = load_daily_dir(data_dir)
    n_symbols = len(bars_by_symbol)
    n_days = len({b.date for bars in bars_by_symbol.values() for b in bars})
    print(f"데이터: {n_days}거래일, 종목 {n_symbols}개 ({data_dir})")
    if n_symbols == 0:
        print("판정 불가: 일봉 데이터가 없습니다. 먼저 수집하세요: "
              "python scripts/collect_daily.py --universe-file universe_kospi200.csv")
        return 2

    base_config = load_config(args.config)
    strategy_dict = load_config(args.strategy_config) if Path(args.strategy_config).exists() else {}
    cfg = RegimeBreakoutConfig.from_config(strategy_dict)
    regime_active = cfg.regime_symbol in bars_by_symbol

    result = swing_walk_forward(
        bars_by_symbol, cfg, base_config,
        capital=args.capital, n_folds=args.folds,
        backtester_cls=RegimeBreakoutBacktester,
    )
    gates = {}
    if args.min_trades is not None:
        gates["min_oos_trades"] = args.min_trades
    v = verdict(result["oos_metrics"], gates or None)
    report = render_swing_report(result, v, data_dir=data_dir,
                                 regime_active=regime_active, capital=args.capital,
                                 title="레짐 조건부 돌파 전략 검증 리포트")
    print(report)

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute() and str(resolve_repo_path(args.data_dir)) != args.data_dir:
        from src.config_loader import REPO_ROOT
        out_dir = REPO_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"regime-breakout-report-{stamp}.md").write_text(report, encoding="utf-8")
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
    (out_dir / f"regime-breakout-report-{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"저장: {out_dir}/regime-breakout-report-{stamp}.md / .json")

    return {"PASS": 0, "FAIL": 1}.get(v["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
