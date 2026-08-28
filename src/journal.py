"""
journal.py — CSV 매매일지 자동 기록
컬럼 스키마: timestamp, symbol, market, side, qty, price, entry_price,
             exit_reason, pnl, pnl_pct, fee, tax, slippage, score, note
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
logger = logging.getLogger("autotrader.journal")

COLUMNS = [
    "timestamp", "symbol", "market", "side", "qty", "price",
    "entry_price", "exit_reason", "pnl", "pnl_pct",
    "fee", "tax", "slippage", "score", "note",
]


class TradeJournal:
    """CSV 매매일지"""

    def __init__(self, journal_dir: str = "data/journal") -> None:
        self.journal_dir = Path(journal_dir)
        self.journal_dir.mkdir(parents=True, exist_ok=True)

    def _get_path(self) -> Path:
        month = datetime.now(tz=KST).strftime("%Y%m")
        return self.journal_dir / f"trades_{month}.csv"

    def _ensure_header(self, path: Path) -> None:
        if not path.exists() or path.stat().st_size == 0:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=COLUMNS)
                writer.writeheader()

    def record(
        self,
        symbol:      str,
        side:        str,
        qty:         int,
        price:       float,
        entry_price: float = 0.0,
        exit_reason: str   = "",
        pnl:         float = 0.0,
        fee:         float = 0.0,
        tax:         float = 0.0,
        slippage:    float = 0.0,
        score:       int   = 0,
        market:      str   = "KRX",
        note:        str   = "",
    ) -> None:
        """매매 1건 기록"""
        path = self._get_path()
        self._ensure_header(path)

        pnl_pct = 0.0
        if entry_price > 0:
            pnl_pct = (price - entry_price) / entry_price * 100

        row = {
            "timestamp":   datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":      symbol,
            "market":      market,
            "side":        side,
            "qty":         qty,
            "price":       round(price, 0),
            "entry_price": round(entry_price, 0),
            "exit_reason": exit_reason,
            "pnl":         round(pnl, 0),
            "pnl_pct":     round(pnl_pct, 4),
            "fee":         round(fee, 0),
            "tax":         round(tax, 0),
            "slippage":    round(slippage, 0),
            "score":       score,
            "note":        note,
        }

        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writerow(row)

        logger.info(
            "매매일지 기록: %s %s %d주 @%.0f PnL=%.0f(%.2f%%)",
            symbol, side, qty, price, pnl, pnl_pct,
        )

    def get_today_trades(self) -> list[dict]:
        """오늘 거래 목록 반환"""
        path = self._get_path()
        if not path.exists():
            return []

        today = datetime.now(tz=KST).strftime("%Y-%m-%d")
        trades = []
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("timestamp", "").startswith(today):
                        trades.append(row)
        except Exception as e:
            logger.error("일지 읽기 실패: %s", e)
        return trades

    def get_recent_trades(self, limit: int = 5) -> list[dict]:
        """최근 거래(SELL) 기록 limit 건 반환"""
        trades = self.get_today_trades()
        sell_trades = [t for t in trades if t.get("side") == "SELL"]
        # Float conversion for sorting and filtering if necessary, but preserving order is enough if they are appended chronologically.
        return sell_trades[-limit:] if sell_trades else []

    def get_daily_summary(self) -> dict:
        """오늘 거래 요약"""
        trades = self.get_today_trades()
        sell_trades = [t for t in trades if t.get("side") == "SELL"]

        total_pnl  = sum(float(t.get("pnl", 0)) for t in sell_trades)
        total_fee  = sum(float(t.get("fee", 0)) for t in sell_trades)
        total_tax  = sum(float(t.get("tax", 0)) for t in sell_trades)
        wins  = sum(1 for t in sell_trades if float(t.get("pnl", 0)) > 0)
        total = len(sell_trades)

        return {
            "trades":    total,
            "wins":      wins,
            "losses":    total - wins,
            "win_rate":  wins / total * 100 if total > 0 else 0.0,
            "total_pnl": total_pnl,
            "total_fee": total_fee,
            "total_tax": total_tax,
            "net_pnl":   total_pnl - total_fee - total_tax,
        }
