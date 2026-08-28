import csv, os

f = r'e:\AI\autotrader\data\journal'
files = sorted([x for x in os.listdir(f) if x.startswith('trades_')])
latest = files[-1] if files else None
print('Latest:', latest)
if latest:
    with open(os.path.join(f, latest), encoding='utf-8-sig') as fp:
        rows = list(csv.DictReader(fp))
    print(f'Total trades: {len(rows)}')
    for r in rows[-15:]:
        et = r.get("entry_time", "")
        xt = r.get("exit_time", "")
        sym = r.get("symbol", "")
        ep = r.get("entry_price", "")
        xp = r.get("exit_price", "")
        pnl = r.get("pnl", "")
        reason = r.get("exit_reason", "")
        print(f"{et} -> {xt} | {sym} | entry={ep} exit={xp} | pnl={pnl} | reason={reason}")
