import os
import sys
import yaml
from pathlib import Path
from dotenv import load_dotenv

sys.path.append(str(Path(__file__).parent.parent.resolve()))

from src.backtest import MultiDayBacktester, load_candles_csv

def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def run_backtest_with_config(config: dict, initial_capital: float = 10_000_000) -> MultiDayBacktester:
    base_dir = Path("data/candles")
    if not base_dir.exists():
        print("Error: data/candles directory does not exist.")
        return None

    # Find and sort all date directories
    date_dirs = sorted([d for d in base_dir.iterdir() if d.is_dir() and d.name.isdigit()])
    print(f"Found {len(date_dirs)} date directories.")

    data_by_date = {}
    for d in date_dirs:
        date_str = d.name
        data_by_date[date_str] = {}
        for csv_path in d.glob("*.csv"):
            symbol = csv_path.stem
            # Load candles
            candles = load_candles_csv(csv_path)
            if candles:
                data_by_date[date_str][symbol] = candles

    print("Initializing MultiDayBacktester...")
    mbt = MultiDayBacktester(config, initial_capital=initial_capital)
    print("Running backtest...")
    mbt.run(data_by_date)
    return mbt

def main():
    config = load_config()
    # Set risk_per_trade_pct or check config
    mbt = run_backtest_with_config(config)
    if mbt:
        report = mbt.report()
        print(report)
        
        # Save report
        out_dir = Path("data/backtest_results")
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "latest_report.txt", "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Saved report to {out_dir / 'latest_report.txt'}")

if __name__ == "__main__":
    main()
