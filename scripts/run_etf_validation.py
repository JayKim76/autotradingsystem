"""
run_etf_validation.py — ETF 듀얼모멘텀 워크포워드 검증 CLI (Phase 1)

사용법:
    python scripts/collect_daily.py --universe-file universe_etf.csv --dir data/etf_daily --years 10
    python scripts/run_etf_validation.py                     # data/etf_daily 기준
    python scripts/run_etf_validation.py --data-dir data/index_daily   # 기초지수 대체 데이터

주의 (사전등록-ETF듀얼모멘텀.md):
  - 본 판정은 기초지수 1990~2012 데이터로만 한다. ETF 상장 이후 구간은
    참고용이며 PASS/FAIL 증거로 쓰지 않는다.
  - 결과는 data/validation/ 아래 etf-report-*.md/.json 으로 저장된다.
판정: PASS(0) / FAIL(1) / INSUFFICIENT·데이터부족(2)
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.resolve()))

from src.config_loader import load_config, resolve_repo_path  # noqa: E402
from src.etf_momentum import EtfMomentumBacktester, EtfMomentumConfig  # noqa: E402
from src.swing import load_daily_dir, render_swing_report, swing_walk_forward  # noqa: E402
from src.validation import verdict  # noqa: E402

WARMUP_DAYS = 252  # 12개월 모멘텀 룩백


def main() -> int:
    p = argparse.ArgumentParser(description="ETF 듀얼모멘텀 워크포워드 검증")
    p.add_argument("--data-dir", default="data/etf_daily")
    p.add_argument("--config", default="config.yaml", help="기본 설정 (미사용 — 호환용)")
    p.add_argument("--etf-config", default="config_etf_momentum.yaml")
    p.add_argument("--capital", type=float, default=10_000_000)
    p.add_argument("--folds", type=int, default=4)
    p.add_argument("--min-trades", type=int, default=None,
                   help="OOS 최소 세그먼트 수 게이트 (기본 30)")
    p.add_argument("--out-dir", default="data/validation")
    args = p.parse_args()

    data_dir = str(resolve_repo_path(args.data_dir))
    bars_by_symbol = load_daily_dir(data_dir)
    n_symbols = len(bars_by_symbol)
    n_days = len({b.date for bars in bars_by_symbol.values() for b in bars})
    print(f"데이터: {n_days}거래일, 종목 {n_symbols}개 ({data_dir})")
    if n_symbols == 0:
        print("판정 불가: 일봉 데이터가 없습니다. 먼저 수집하세요: "
              "python scripts/collect_daily.py --universe-file universe_etf.csv "
              "--dir data/etf_daily --years 10")
        return 2

    base_config = load_config(args.config)
    etf_dict = load_config(args.etf_config) if Path(args.etf_config).exists() else {}
    cfg = EtfMomentumConfig.from_config(etf_dict)
    missing = [s for s in cfg.risk_symbols + [cfg.defensive_symbol]
               if s not in bars_by_symbol]
    if missing:
        print(f"참고: 유니버스 중 데이터 없는 종목 {missing} — 있는 종목만으로 진행합니다")

    result = swing_walk_forward(
        bars_by_symbol, cfg, base_config,
        capital=args.capital, n_folds=args.folds,
        warmup_days=WARMUP_DAYS,
        backtester_cls=EtfMomentumBacktester,
    )
    gates = {}
    if args.min_trades is not None:
        gates["min_oos_trades"] = args.min_trades
    v = verdict(result["oos_metrics"], gates or None)
    report = render_swing_report(result, v, data_dir=data_dir,
                                 regime_active=False, capital=args.capital,
                                 title="ETF 듀얼모멘텀 검증 리포트 (Phase 1)")
    print(report)

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        from src.config_loader import REPO_ROOT
        out_dir = REPO_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"etf-report-{stamp}.md").write_text(report, encoding="utf-8")
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
    (out_dir / f"etf-report-{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"저장: {out_dir}/etf-report-{stamp}.md / .json")

    return {"PASS": 0, "FAIL": 1}.get(v["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
