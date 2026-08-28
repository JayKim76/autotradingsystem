import os
import sys
import time
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

sys.path.append(str(Path(__file__).parent.parent.resolve()))

from src.auth import KISAuth
from src.kis_client import KISClient
from src.indicators import parse_candles

# Target symbols to collect candles for
SYMBOLS = [
    # Fallback universe (active mid/large growth stocks)
    "247540", "086520", "196170", "028300", "022100", 
    "095340", "058470", "277810", "348370", "137400", 
    "240810", "042700", "403870", "294090",
    # Active journal symbols & major stocks
    "005930", "000660", "073240", "251340", "229200", 
    "047770", "025980", "001510", "006340", "002990", 
    "0167A0", "950260", "0220W0"
]

def main():
    load_dotenv()
    auth = KISAuth()
    client = KISClient(auth)

    print("Fetching trading days from daily candles...")
    # Fetch 100 daily candles of Samsung Electronics to extract trading dates
    daily = client.get_daily_candles("005930", period="D")
    if not daily:
        print("Error: Could not retrieve daily candles to extract dates.")
        return
        
    dates = [r.get("stck_bsop_date") for r in daily]
    dates = sorted(list(set(filter(None, dates))))
    
    # Keep the most recent 60 trading days
    target_dates = dates[-60:]
    print(f"Targeting {len(target_dates)} trading days: {target_dates[0]} to {target_dates[-1]}")

    base_dir = Path("data/candles")
    base_dir.mkdir(parents=True, exist_ok=True)

    success_count = 0
    skip_count = 0
    fail_count = 0

    total_tasks = len(target_dates) * len(SYMBOLS)
    task_idx = 0

    start_time = time.time()

    for date_str in target_dates:
        date_dir = base_dir / date_str
        date_dir.mkdir(parents=True, exist_ok=True)

        for symbol in SYMBOLS:
            task_idx += 1
            csv_path = date_dir / f"{symbol}.csv"

            # Skip if file already exists and is non-empty
            if csv_path.exists() and csv_path.stat().st_size > 50:
                skip_count += 1
                continue

            print(f"[{task_idx}/{total_tasks}] Fetching {symbol} for {date_str}...")
            try:
                # Retrieve full day's candles (09:00 to 15:30)
                rows = client.get_historical_minute_candles_full(symbol, date_str)
                # Parse to Candle list (bar_minutes=1 to keep raw minute candles)
                candles = parse_candles(rows, exclude_forming=False, bar_minutes=1)
                
                if not candles:
                    print(f"  Warning: No candles found for {symbol} on {date_str}")
                    fail_count += 1
                    # Create an empty file or write a tiny header to avoid retrying continuously
                    with open(csv_path, "w", encoding="utf-8") as f:
                        f.write("time,open,high,low,close,volume\n")
                    continue

                # Write to CSV
                with open(csv_path, "w", encoding="utf-8") as f:
                    f.write("time,open,high,low,close,volume\n")
                    for c in sorted(candles, key=lambda x: x.time):
                        # Ensure time has leading zeros / formatted properly (HHMMSS)
                        t = c.time.replace(":", "")
                        if len(t) == 4:
                            t += "00"
                        f.write(f"{t},{c.open:.1f},{c.high:.1f},{c.low:.1f},{c.close:.1f},{c.volume}\n")
                
                success_count += 1
                print(f"  Success: Saved {len(candles)} candles to {csv_path}")
            except Exception as e:
                print(f"  Failed: Error fetching {symbol} on {date_str}: {e}")
                fail_count += 1

            # Sleep slightly to prevent hitting KIS rate limits too hard
            time.sleep(0.1)

    elapsed = time.time() - start_time
    print("\n" + "="*50)
    print("Candle Data Collection Completed")
    print(f"Total time elapsed: {elapsed:.1f} seconds")
    print(f"Successful downloads: {success_count}")
    print(f"Skipped (already exists): {skip_count}")
    print(f"Failed downloads: {fail_count}")
    print("="*50)

if __name__ == "__main__":
    main()
