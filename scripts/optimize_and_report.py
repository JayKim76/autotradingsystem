import os
import sys
import copy
import yaml
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.resolve()))

from scripts.run_backtest import run_backtest_with_config, load_config

def main():
    base_config = load_config()

    scenarios = {
        "Baseline (Current Config)": {},
        
        "Low Frequency (min_score=4, max_trades=3)": {
            "strategy": {"min_score": 4},
            "risk": {"max_daily_trades": 3}
        },
        
        "No Average Down (avg_down=False)": {
            "scale_in": {"avg_down_enabled": False}
        },
        
        "Higher ATR Stop (atr_mult=2.0)": {
            "stop_loss": {"atr_multiplier": 2.0}
        },
        
        "Combined Optimal (Score=4, MaxTrades=3, NoAvgDown, ATR=2.0)": {
            "strategy": {"min_score": 4},
            "risk": {"max_daily_trades": 3},
            "scale_in": {"avg_down_enabled": False},
            "stop_loss": {"atr_multiplier": 2.0}
        },
        
        "Strict High-Yield (Score=5, MaxTrades=2, MinDip=2.5%, TP1=2.0R, ATR=2.0, NoAvgDown)": {
            "strategy": {"min_score": 5, "min_dip_from_high_pct": 2.5},
            "risk": {"max_daily_trades": 2},
            "take_profit": {"tp1_r": 2.0, "tp2_r": 3.5},
            "scale_in": {"avg_down_enabled": False},
            "stop_loss": {"atr_multiplier": 2.0}
        },

        "v1.1 Breakout (ORB=True, VWAP=True, Pullback=False)": {
            "strategy": {
                "require_orb_breakout": True,
                "require_vwap_above": True,
                "require_pullback": False,
                "min_score": 3
            },
            "risk": {"max_daily_trades": 3},
            "scale_in": {"avg_down_enabled": False},
            "stop_loss": {"atr_multiplier": 2.0}
        },

        "Super-Strict Pullback (Score=5, MaxTrades=1, MinDip=3.0%, TP1=2.0R, ATR=2.0, NoAvgDown)": {
            "strategy": {"min_score": 5, "min_dip_from_high_pct": 3.0},
            "risk": {"max_daily_trades": 1},
            "take_profit": {"tp1_r": 2.0, "tp2_r": 3.5},
            "scale_in": {"avg_down_enabled": False},
            "stop_loss": {"atr_multiplier": 2.0}
        }
    }

    results = []

    for name, overrides in scenarios.items():
        print(f"\n==========================================")
        print(f"Running Scenario: {name}")
        print(f"==========================================")
        
        # Clone base config
        config = copy.deepcopy(base_config)
        
        # Apply overrides
        for sec, fields in overrides.items():
            if sec not in config:
                config[sec] = {}
            for k, v in fields.items():
                config[sec][k] = v

        mbt = run_backtest_with_config(config)
        if mbt:
            results.append({
                "name": name,
                "return_pct": mbt.return_pct,
                "net_pnl": mbt.total_pnl,
                "trades": len(mbt.all_trades),
                "win_rate": mbt.trade_win_rate,
                "profit_factor": mbt.profit_factor,
                "mdd": mbt.max_drawdown_pct,
                "sharpe": mbt.sharpe,
                "config": config
            })

    # Print comparison table
    print("\n\n" + "="*80)
    print("SCENARIO COMPARISON RESULT")
    print("="*80)
    
    header = f"{'Scenario Name':<45} | {'Return %':>9} | {'Trades':>6} | {'Win%':>6} | {'PF':>6} | {'MDD%':>6} | {'Sharpe':>6}"
    print(header)
    print("-"*len(header))
    
    for r in results:
        line = (
            f"{r['name']:<45} | "
            f"{r['return_pct']:>8.2f}% | "
            f"{r['trades']:>6} | "
            f"{r['win_rate']:>5.1f}% | "
            f"{r['profit_factor']:>6.2f} | "
            f"{r['mdd']:>5.2f}% | "
            f"{r['sharpe']:>6.2f}"
        )
        print(line)
    print("="*80)

    # Save comparison as markdown
    out_dir = Path("data/backtest_results")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    md_lines = [
        "# Strategy Optimization Scenario Comparison",
        "",
        "| Scenario Name | Return % | Total Trades | Win Rate % | Profit Factor | MDD % | Sharpe Ratio |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        md_lines.append(
            f"| {r['name']} | {r['return_pct']:.2f}% | {r['trades']} | {r['win_rate']:.1f}% | {r['profit_factor']:.2f} | {r['mdd']:.2f}% | {r['sharpe']:.2f} |"
        )
    
    with open(out_dir / "comparison.md", "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))
    print(f"Saved comparison Markdown to {out_dir / 'comparison.md'}")

    # Determine best config (highest Return with acceptable MDD <= 20%)
    best_scenario = None
    best_return = -9999.0
    for r in results:
        if r["return_pct"] > best_return and r["mdd"] <= 20.0:
            best_return = r["return_pct"]
            best_scenario = r

    # Fallback to the one with the highest Return
    if not best_scenario and results:
        best_scenario = max(results, key=lambda x: x["return_pct"])

    if best_scenario:
        print(f"\nBest Performing Scenario: {best_scenario['name']} (Return: {best_scenario['return_pct']:.2f}%, MDD: {best_scenario['mdd']:.2f}%)")
        # Save optimized config
        with open("config_optimized.yaml", "w", encoding="utf-8") as f:
            yaml.dump(best_scenario["config"], f, default_flow_style=False, allow_unicode=True)
        print("Saved best configuration to config_optimized.yaml")

if __name__ == "__main__":
    main()
