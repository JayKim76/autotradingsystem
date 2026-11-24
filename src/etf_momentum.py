"""
etf_momentum.py — 멀티에셋 ETF 듀얼모멘텀 (Phase 1)

설계·사전등록: ETF듀얼모멘텀설계.md / 사전등록-ETF듀얼모멘텀.md
  - 월 1회 리밸런싱. 신호는 월말 종가, 체결은 다음 거래일 시가 (미래참조 없음)
  - 상대모멘텀: 위험자산 252일 수익률 상위 top_n
  - 절대모멘텀: 방어자산보다 못하면 그 슬롯은 방어자산으로 이동
  - 비용은 실제 거래금액(턴오버)에 편도 cost_pct_oneway 부과
  - 보유는 "종목 × 1개월" 세그먼트로 BTTrade 기록 → validation 지표 재사용
  - 1R = 포지션 금액 × 진입 시 월간 변동성 (60일 일간수익률 표준편차 × √21)
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from .backtest import BTTrade
from .swing import DailyBar

logger = logging.getLogger("autotrader.etf_momentum")

# 1R 하한 (%): 변동성 추정이 0에 가까우면 R이 폭발해 지표가 무의미해진다.
# 월간 변동성 0.5% 미만은 현실적으로 없으므로 하한을 둔다 (지표 안정화용).
MIN_R_UNIT_PCT = 0.5

DEFAULT_RISK_SYMBOLS = ["069500", "360750", "379800", "182490", "132030"]
DEFAULT_DEFENSIVE = "153130"


@dataclass
class EtfMomentumConfig:
    risk_symbols: list[str] = field(default_factory=lambda: list(DEFAULT_RISK_SYMBOLS))
    defensive_symbol: str = DEFAULT_DEFENSIVE
    lookback_days: int = 252          # 12개월 (문헌 표준값)
    top_n: int = 2                    # 슬롯 수 — 슬롯당 비중 = 1/top_n
    cost_pct_oneway: float = 0.05     # 편도 비용 % (수수료+슬리피지, 거래세 0 가정)
    vol_window_days: int = 60         # R 환산용 변동성 추정 구간

    @classmethod
    def from_config(cls, cfg: dict) -> "EtfMomentumConfig":
        sec = (cfg or {}).get("etf_momentum", {}) or {}
        out = cls()
        for key in ("risk_symbols", "defensive_symbol", "lookback_days",
                    "top_n", "cost_pct_oneway", "vol_window_days"):
            if key in sec and sec[key] is not None:
                setattr(out, key, sec[key])
        out.risk_symbols = [str(s).zfill(6) if str(s).isdigit() else str(s)
                            for s in out.risk_symbols]
        out.defensive_symbol = str(out.defensive_symbol)
        if out.defensive_symbol.isdigit():
            out.defensive_symbol = out.defensive_symbol.zfill(6)
        return out


# ──────────────────────────────────────────────
#  순수 판정 함수 (테스트 대상)
# ──────────────────────────────────────────────

def momentum_return(closes: list[float], idx: int, lookback: int) -> Optional[float]:
    """idx 시점의 lookback일 수익률. 히스토리 부족이면 None."""
    if idx < lookback or idx >= len(closes):
        return None
    base = closes[idx - lookback]
    if base <= 0:
        return None
    return closes[idx] / base - 1.0


def select_targets(risk_returns: dict[str, float], defensive_return: float,
                   top_n: int) -> list[str]:
    """상대 순위 상위 top_n 중 절대모멘텀(방어자산 초과)을 통과한 종목.

    반환 순서 = 순위 순서. 통과 종목이 top_n보다 적으면 그만큼 방어 슬롯.
    """
    ranked = sorted(risk_returns.items(), key=lambda kv: kv[1], reverse=True)
    return [sym for sym, r in ranked[:top_n] if r > defensive_return]


def target_weights(risk_returns: dict[str, float], defensive_return: float,
                   defensive_symbol: str, top_n: int) -> dict[str, float]:
    """최종 목표 비중 {종목: 0~1}. 슬롯 비중은 1/top_n, 잔여는 방어자산."""
    slot = 1.0 / top_n if top_n > 0 else 0.0
    selected = select_targets(risk_returns, defensive_return, top_n)
    weights = {sym: slot for sym in selected}
    defensive_weight = 1.0 - slot * len(selected)
    if defensive_weight > 1e-9:
        weights[defensive_symbol] = weights.get(defensive_symbol, 0.0) + defensive_weight
    return weights


def monthly_vol_pct(closes: list[float], idx: int, window: int = 60) -> float:
    """idx 직전 window일 일간수익률 표준편차 × √21 → 월간 변동성 %. 부족하면 0."""
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
    return math.sqrt(var) * math.sqrt(21) * 100.0


# ──────────────────────────────────────────────
#  백테스터 (swing_walk_forward 호환 인터페이스)
# ──────────────────────────────────────────────

class _Series:
    def __init__(self, bars: list[DailyBar]):
        self.bars = bars
        self.index = {b.date: i for i, b in enumerate(bars)}
        self.closes = [b.close for b in bars]


class _Segment:
    def __init__(self, symbol: str, entry_date: str, entry_price: float,
                 qty: int, entry_cost: float, r_unit_pct: float):
        self.symbol = symbol
        self.entry_date = entry_date
        self.entry_price = entry_price
        self.qty = qty
        self.entry_cost = entry_cost
        self.r_unit_pct = r_unit_pct


class EtfMomentumBacktester:
    """월간 리밸런싱 포트폴리오 백테스터.

    swing_walk_forward(bars, cfg, base_config, backtester_cls=...)와
    같은 인터페이스: __init__(bars_by_symbol, cfg, base_config),
    .all_dates, .run(capital, window) → {trades, equity, end_capital}
    비용 모델은 base_config가 아니라 cfg.cost_pct_oneway를 쓴다
    (사전등록된 편도 0.05% — config.yaml의 개별주 비용과 혼동 방지).
    """

    def __init__(self, bars_by_symbol: dict[str, list[DailyBar]],
                 cfg: EtfMomentumConfig, base_config: dict):
        self.cfg = cfg
        self.series: dict[str, _Series] = {}
        for symbol, bars in bars_by_symbol.items():
            if bars:
                self.series[symbol] = _Series(sorted(bars, key=lambda b: b.date))
        self.all_dates = sorted({b.date for s in self.series.values() for b in s.bars})

    # ── 리밸런싱 일정 ──
    def _rebalance_signal_dates(self) -> set[str]:
        """각 월의 마지막 거래일 집합 (신호일). 실행은 다음 거래일 시가."""
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
        if not dates:
            return {"trades": [], "equity": [], "end_capital": capital}

        signal_dates = self._rebalance_signal_dates()
        pending_weights: Optional[dict[str, float]] = None  # 다음날 시가 실행 대기
        cash = capital
        segments: dict[str, _Segment] = {}
        trades: list[BTTrade] = []
        equity: list[tuple[str, float]] = []

        def price_at(symbol: str, date: str, field_name: str) -> Optional[float]:
            s = self.series.get(symbol)
            if s is None:
                return None
            j = s.index.get(date)
            if j is None:
                return None
            return getattr(s.bars[j], field_name)

        def close_segment(seg: _Segment, date: str, raw_price: float,
                          exit_cost: float, reason: str) -> None:
            gross = (raw_price - seg.entry_price) * seg.qty
            trades.append(BTTrade(
                symbol=seg.symbol, entry_time=seg.entry_date, exit_time=date,
                entry_price=seg.entry_price, exit_price=raw_price, qty=seg.qty,
                reason=reason, gross=gross, cost=seg.entry_cost + exit_cost,
                date=date, r_unit_pct=seg.r_unit_pct,
            ))

        for di, date in enumerate(dates):
            is_last = di == len(dates) - 1

            # 1) 전일 월말 신호가 있으면 오늘 시가에 리밸런싱 실행
            if pending_weights is not None and not is_last:
                # 포트폴리오 평가 (시가 기준)
                total = cash
                open_px: dict[str, float] = {}
                for sym, seg in segments.items():
                    px = price_at(sym, date, "open")
                    if px is None:
                        px = seg.entry_price
                    open_px[sym] = px
                    total += seg.qty * px
                for sym in pending_weights:
                    if sym not in open_px:
                        px = price_at(sym, date, "open")
                        if px is not None:
                            open_px[sym] = px

                # 목표 수량 계산
                targets: dict[str, int] = {}
                for sym, w in pending_weights.items():
                    px = open_px.get(sym)
                    if px and px > 0:
                        targets[sym] = int((total * w) // px)

                # 거래금액(턴오버) 기준 비용 — 유지 종목의 불변분은 비용 없음.
                # 기록 회계: 종목별 거래금액 비용을 닫는 세그먼트와 여는
                # 세그먼트가 절반씩 나눠 갖는다 (한쪽만 있으면 그쪽이 전부).
                # 기록된 비용 합계 = 실제 현금 차감액과 일치한다.
                all_syms = set(segments) | set(targets)
                traded_amt: dict[str, float] = {}
                for sym in all_syms:
                    px = open_px.get(sym)
                    if not px:
                        continue
                    cur = segments[sym].qty if sym in segments else 0
                    traded_amt[sym] = abs(targets.get(sym, 0) - cur) * px
                total_cost = sum(traded_amt.values()) * cfg.cost_pct_oneway / 100.0

                held_before = set(segments)
                for sym in list(segments):
                    seg = segments.pop(sym)
                    px = open_px.get(sym, seg.entry_price)
                    cost_i = traded_amt.get(sym, 0.0) * cfg.cost_pct_oneway / 100.0
                    exit_cost = cost_i if targets.get(sym, 0) <= 0 else cost_i / 2.0
                    close_segment(seg, date, px, exit_cost, "월간리밸런싱")
                    cash += seg.qty * px

                # 비용 차감 후 신규 세그먼트 개시
                cash -= total_cost
                spent = sum(tq * open_px[s] for s, tq in targets.items() if tq > 0)
                if spent > cash and spent > 0:
                    # 잔고 부족 시 비례 축소 (수수료 차감 영향)
                    ratio = cash / spent
                    targets = {s: int(tq * ratio) for s, tq in targets.items()}
                    spent = sum(tq * open_px[s] for s, tq in targets.items() if tq > 0)
                for sym, tq in targets.items():
                    if tq <= 0:
                        continue
                    px = open_px[sym]
                    cost_i = traded_amt.get(sym, 0.0) * cfg.cost_pct_oneway / 100.0
                    # 닫힌 세그먼트가 있던 종목은 진입 비용 절반만 부담
                    # (나머지 절반은 닫힌 세그먼트의 청산 비용으로 기록됨)
                    entry_cost = cost_i / 2.0 if sym in held_before else cost_i
                    s = self.series[sym]
                    j = s.index.get(date)
                    r_unit = monthly_vol_pct(s.closes, j, cfg.vol_window_days) if j is not None else 0.0
                    r_unit = max(r_unit, MIN_R_UNIT_PCT)
                    segments[sym] = _Segment(sym, date, px, tq, entry_cost, r_unit)
                cash -= spent
                pending_weights = None

            # 2) 마지막 날: 종가 강제청산
            if is_last:
                for sym in list(segments):
                    seg = segments.pop(sym)
                    px = price_at(sym, date, "close")
                    if px is None:
                        px = seg.entry_price
                    close_segment(seg, date, px, 0.0, "기간종료")
                    cash += seg.qty * px
                equity.append((date, cash))
                break

            # 3) 월말이면 신호 계산 (체결은 다음 거래일 시가)
            if date in signal_dates:
                risk_returns: dict[str, float] = {}
                for sym in cfg.risk_symbols:
                    s = self.series.get(sym)
                    if s is None:
                        continue
                    j = s.index.get(date)
                    if j is None:
                        continue
                    r = momentum_return(s.closes, j, cfg.lookback_days)
                    if r is not None:
                        risk_returns[sym] = r
                ds = self.series.get(cfg.defensive_symbol)
                d_ret: Optional[float] = None
                if ds is not None:
                    dj = ds.index.get(date)
                    if dj is not None:
                        d_ret = momentum_return(ds.closes, dj, cfg.lookback_days)
                if risk_returns and d_ret is not None:
                    pending_weights = target_weights(
                        risk_returns, d_ret, cfg.defensive_symbol, cfg.top_n)

            # 4) 종가 평가
            total_close = cash
            for sym, seg in segments.items():
                px = price_at(sym, date, "close")
                total_close += seg.qty * (px if px is not None else seg.entry_price)
            equity.append((date, total_close))

        end_capital = equity[-1][1] if equity else capital
        return {"trades": trades, "equity": equity, "end_capital": end_capital}
