"""
futures_tsmom.py — KOSPI200 선물 시계열 모멘텀 (Phase 2)

설계·사전등록: 선물TSMOM설계.md / 사전등록-선물TSMOM.md
  - 월 1회 리밸런싱. 신호(252일 수익률 부호)는 월말 종가, 체결은 다음 거래일 시가
  - 변동성 타게팅: 배율 = min(목표 연변동성 / 실현변동성(60일), 상한)
  - 비용: 명목금액 변동분 편도 cost_pct_oneway + 분기 롤월 포지션 유지 시 추가
  - 보유는 1개월 세그먼트로 BTTrade 기록 → validation 지표 재사용
  - 1R = 진입 명목금액 × 월간 변동성. BTTrade 인코딩: entry_price=지수 레벨,
    qty=1, r_unit_pct = (1R 금액 / 레벨) × 100
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from .backtest import BTTrade
from .swing import DailyBar

logger = logging.getLogger("autotrader.futures_tsmom")

ROLL_MONTHS = (3, 6, 9, 12)  # KOSPI200 선물 분기 만기월


@dataclass
class FuturesTsmomConfig:
    symbol: str = "KOSPI200F"          # data/futures_daily/<symbol>.csv
    lookback_days: int = 252           # 12개월 (문헌 표준값)
    target_vol_annual: float = 0.10    # 목표 연변동성 10%
    vol_window_days: int = 60
    max_leverage: float = 2.0
    cost_pct_oneway: float = 0.01      # 편도 비용 % (보수적 가정)

    @classmethod
    def from_config(cls, cfg: dict) -> "FuturesTsmomConfig":
        sec = (cfg or {}).get("futures_tsmom", {}) or {}
        out = cls()
        for key in ("symbol", "lookback_days", "target_vol_annual",
                    "vol_window_days", "max_leverage", "cost_pct_oneway"):
            if key in sec and sec[key] is not None:
                setattr(out, key, sec[key])
        return out


# ──────────────────────────────────────────────
#  순수 판정 함수 (테스트 대상)
# ──────────────────────────────────────────────

def realized_vol_annual(closes: list[float], idx: int, window: int = 60) -> float:
    """idx 직전 window일 일간수익률 표준편차 × √252 → 연변동성(비율). 부족하면 0."""
    if idx < 2:
        return 0.0
    start = max(1, idx - window + 1)
    rets = []
    for i in range(start, idx + 1):
        if closes[i - 1] > 0:
            rets.append(closes[i] / closes[i - 1] - 1.0)
    if len(rets) < 10:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def tsmom_direction(closes: list[float], idx: int, lookback: int = 252) -> int:
    """252일 수익률 부호 → +1(롱) / -1(숏) / 0(판정 불가)"""
    if idx < lookback or idx >= len(closes):
        return 0
    base = closes[idx - lookback]
    if base <= 0:
        return 0
    ret = closes[idx] / base - 1.0
    if ret > 0:
        return 1
    if ret < 0:
        return -1
    return 0


def vol_target_weight(realized_vol: float, target_vol: float = 0.10,
                      max_leverage: float = 2.0) -> float:
    """변동성 타게팅 배율. 실현변동성 0이면 0 (포지션 없음)."""
    if realized_vol <= 0:
        return 0.0
    return min(target_vol / realized_vol, max_leverage)


# ──────────────────────────────────────────────
#  백테스터 (swing_walk_forward 호환)
# ──────────────────────────────────────────────

class _Segment:
    def __init__(self, entry_date: str, entry_price: float, direction: int,
                 notional: float, entry_cost: float, r_unit_pct: float):
        self.entry_date = entry_date
        self.entry_price = entry_price
        self.direction = direction
        self.notional = notional
        self.entry_cost = entry_cost
        self.r_unit_pct = r_unit_pct


class FuturesTsmomBacktester:
    """단일 시리즈 월간 TSMOM 백테스터.

    bars_by_symbol에서 cfg.symbol을 찾고, 없으면 시리즈가 1개뿐일 때 그걸 쓴다.
    자본 모델: 증거금 계좌가 아니라 자본 전액이 담보인 명목금액 모델
    (equity = 자본 + 누적 손익, 일별 종가 평가).
    """

    def __init__(self, bars_by_symbol: dict[str, list[DailyBar]],
                 cfg: FuturesTsmomConfig, base_config: dict):
        self.cfg = cfg
        bars = bars_by_symbol.get(cfg.symbol)
        if bars is None and len(bars_by_symbol) == 1:
            bars = next(iter(bars_by_symbol.values()))
        bars = sorted(bars or [], key=lambda b: b.date)
        self.bars = bars
        self.index = {b.date: i for i, b in enumerate(bars)}
        self.closes = [b.close for b in bars]
        self.all_dates = [b.date for b in bars]

    def _signal_dates(self) -> set[str]:
        out: set[str] = set()
        for i, d in enumerate(self.all_dates):
            nxt = self.all_dates[i + 1] if i + 1 < len(self.all_dates) else None
            if nxt is None or nxt[:6] != d[:6]:
                out.add(d)
        return out

    def run(self, capital: float = 10_000_000,
            window: Optional[tuple[str, str]] = None) -> dict:
        cfg = self.cfg
        dates = self.all_dates
        if window:
            dates = [d for d in dates if window[0] <= d <= window[1]]
        if not dates or not self.bars:
            return {"trades": [], "equity": [], "end_capital": capital}

        signal_dates = self._signal_dates()
        pending: Optional[tuple[int, float]] = None  # (방향, 배율) 다음날 시가 실행
        seg: Optional[_Segment] = None
        realized_pnl = 0.0  # 종료된 세그먼트 누적 손익 (비용 차감 후)
        trades: list[BTTrade] = []
        equity: list[tuple[str, float]] = []

        def close_segment(s: _Segment, date: str, exit_price: float,
                          exit_cost: float, reason: str) -> float:
            ret = (exit_price - s.entry_price) / s.entry_price if s.entry_price > 0 else 0.0
            gross = s.direction * ret * s.notional
            trades.append(BTTrade(
                symbol=cfg.symbol, entry_time=s.entry_date, exit_time=date,
                entry_price=s.entry_price, exit_price=exit_price, qty=1,
                reason=reason, gross=gross, cost=s.entry_cost + exit_cost,
                date=date, r_unit_pct=s.r_unit_pct,
            ))
            return gross - s.entry_cost - exit_cost

        for di, date in enumerate(dates):
            j = self.index[date]
            bar = self.bars[j]
            is_last = di == len(dates) - 1
            equity_now = capital + realized_pnl + (
                seg.direction * (bar.open - seg.entry_price) / seg.entry_price * seg.notional
                - seg.entry_cost if seg else 0.0)

            # 1) 전일 신호 실행 (오늘 시가)
            if pending is not None and not is_last:
                direction, weight = pending
                new_notional = equity_now * weight if direction != 0 else 0.0
                old_notional = seg.notional if seg else 0.0
                # 거래 명목금액: 신규=전액, 반전=청산+신규 합산, 유지=변동분만
                if seg is None:
                    traded = new_notional
                elif seg.direction != direction:
                    traded = old_notional + new_notional
                else:
                    traded = abs(new_notional - old_notional)
                cost = traded * cfg.cost_pct_oneway / 100.0
                # 분기 롤월: 포지션을 유지/반전하며 들고 넘어가면 롤 비용 추가
                if seg and int(date[4:6]) in ROLL_MONTHS and new_notional > 0:
                    cost += new_notional * cfg.cost_pct_oneway / 100.0

                if seg:
                    exit_cost = cost / 2.0 if new_notional > 0 else cost
                    realized_pnl += close_segment(seg, date, bar.open, exit_cost, "월간리밸런싱")
                    seg = None
                else:
                    exit_cost = 0.0

                if new_notional > 0 and direction != 0:
                    entry_cost = cost - exit_cost
                    monthly_vol = realized_vol_annual(self.closes, j, cfg.vol_window_days) / math.sqrt(12)
                    risk_amount = new_notional * monthly_vol
                    # 1R 하한: 명목금액의 0.5% (변동성 추정이 0에 가까울 때 R 폭발 방지)
                    risk_amount = max(risk_amount, new_notional * 0.005)
                    r_unit_pct = (risk_amount / bar.open * 100.0) if bar.open > 0 else 0.0
                    seg = _Segment(date, bar.open, direction, new_notional,
                                   entry_cost, r_unit_pct)
                pending = None

            # 2) 마지막 날 종가 강제청산
            if is_last:
                if seg:
                    realized_pnl += close_segment(seg, date, bar.close, 0.0, "기간종료")
                    seg = None
                equity.append((date, capital + realized_pnl))
                break

            # 3) 월말 신호
            if date in signal_dates:
                direction = tsmom_direction(self.closes, j, cfg.lookback_days)
                vol = realized_vol_annual(self.closes, j, cfg.vol_window_days)
                weight = vol_target_weight(vol, cfg.target_vol_annual, cfg.max_leverage)
                pending = (direction, weight)

            # 4) 종가 평가 (미실현 포함)
            total = capital + realized_pnl
            if seg:
                total += seg.direction * (bar.close - seg.entry_price) / seg.entry_price * seg.notional - seg.entry_cost
            equity.append((date, total))

        end_capital = equity[-1][1] if equity else capital
        return {"trades": trades, "equity": equity, "end_capital": end_capital}
