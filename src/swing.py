"""
swing.py — 일봉 스윙 전략 (추세 눌림목, 보유 2~10일)

설계 원칙 (스윙전략설계.md 참조):
  - 단일 셋업, 파라미터 최소화, 스케일인 없음
  - 신호는 종가 확정 후 생성, 체결은 다음날 시가 (미래참조 없음)
  - 손절은 진입 시 고정. 갭하락은 시가 체결로 보수적으로 반영
  - 검증 지표/판정은 validation.py를 그대로 재사용한다

데이터 포맷:
  - 일봉 CSV: data/daily/<종목>.csv (date,open,high,low,close,volume)
  - 분봉 수집본(data/candles)도 aggregate_daily()로 일봉화해 쓸 수 있다
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .backtest import BTTrade
from .indicators import Candle

logger = logging.getLogger("autotrader.swing")


# ──────────────────────────────────────────────
#  데이터
# ──────────────────────────────────────────────

@dataclass
class DailyBar:
    date: str          # YYYYMMDD
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    @property
    def value_krw(self) -> float:
        """당일 거래대금 근사 (종가 × 거래량)"""
        return self.close * self.volume


def aggregate_daily(minute_data: dict[str, dict[str, list[Candle]]]) -> dict[str, list[DailyBar]]:
    """분봉 수집본 {날짜: {종목: [Candle]}} → {종목: [DailyBar]} (날짜순)"""
    out: dict[str, list[DailyBar]] = {}
    for date in sorted(minute_data):
        for symbol, candles in minute_data[date].items():
            if not candles:
                continue
            ordered = sorted(candles, key=lambda c: c.time)
            out.setdefault(symbol, []).append(DailyBar(
                date=date,
                open=ordered[0].open,
                high=max(c.high for c in ordered),
                low=min(c.low for c in ordered),
                close=ordered[-1].close,
                volume=sum(c.volume for c in ordered),
            ))
    for bars in out.values():
        bars.sort(key=lambda b: b.date)
    return out


def load_daily_dir(base_dir: str | Path) -> dict[str, list[DailyBar]]:
    """data/daily/<종목>.csv 들을 읽어 {종목: [DailyBar]} 반환"""
    base = Path(base_dir)
    out: dict[str, list[DailyBar]] = {}
    if not base.exists():
        return out
    for path in sorted(base.glob("*.csv")):
        bars: list[DailyBar] = []
        with open(path, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    bars.append(DailyBar(
                        date=str(row["date"]).replace("-", ""),
                        open=float(row["open"]), high=float(row["high"]),
                        low=float(row["low"]), close=float(row["close"]),
                        volume=int(float(row.get("volume") or 0)),
                    ))
                except (KeyError, ValueError):
                    continue
        if bars:
            bars.sort(key=lambda b: b.date)
            out[path.stem] = bars
    return out


def save_daily_csv(base_dir: str | Path, symbol: str, bars: list[DailyBar]) -> Path:
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{symbol}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for b in sorted(bars, key=lambda x: x.date):
            w.writerow([b.date, b.open, b.high, b.low, b.close, b.volume])
    return path


# ──────────────────────────────────────────────
#  지표 (일봉 시리즈용, Wilder 방식)
# ──────────────────────────────────────────────

def sma(values: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if period <= 0:
        return out
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= period:
            acc -= values[i - period]
        if i >= period - 1:
            out[i] = acc / period
    return out


def atr_series(bars: list[DailyBar], period: int = 14) -> list[Optional[float]]:
    n = len(bars)
    out: list[Optional[float]] = [None] * n
    if n == 0:
        return out
    trs: list[float] = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            trs.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    if n >= period:
        value = sum(trs[:period]) / period
        out[period - 1] = value
        for i in range(period, n):
            value = (value * (period - 1) + trs[i]) / period
            out[i] = value
    return out


def rsi_series(closes: list[float], period: int = 14) -> list[Optional[float]]:
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if n <= period:
        return out
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def _rsi(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        return 100 - 100 / (1 + ag / al)

    out[period] = _rsi(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi(avg_gain, avg_loss)
    return out


# ──────────────────────────────────────────────
#  설정
# ──────────────────────────────────────────────

@dataclass
class SwingConfig:
    ma_fast: int = 20
    ma_slow: int = 60
    atr_period: int = 14
    rsi_period: int = 14
    rsi_pullback: float = 45.0
    pullback_lookback: int = 3
    stop_atr_mult: float = 2.0
    max_hold_days: int = 10
    risk_pct: float = 0.75            # 거래당 리스크 (자본 %)
    max_position_pct: float = 25.0    # 종목당 최대 비중 (자본 %)
    max_positions: int = 4
    max_entries_per_day: int = 2
    min_avg_value_krw: float = 5_000_000_000   # 20일 평균 거래대금 하한
    gap_up_limit_pct: float = 2.0     # 진입일 갭상승 허용 상한
    gap_down_limit_pct: float = 5.0   # 진입일 갭하락 허용 하한 (밤사이 정보 충격 시 신호 무효)
    regime_symbol: str = "069500"     # KODEX 200 — 레짐 판정용

    @classmethod
    def from_config(cls, config: dict) -> "SwingConfig":
        section = (config or {}).get("swing", {}) or {}
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in section.items() if k in known})


# ──────────────────────────────────────────────
#  백테스터
# ──────────────────────────────────────────────

@dataclass
class _Position:
    symbol: str
    entry_date: str
    entry_price: float     # 슬리피지 반영 체결가
    qty: int
    stop_price: float
    r_unit_pct: float
    entry_cost: float
    entry_index: int       # 보유일수 계산용 (전역 날짜 인덱스)


@dataclass
class _Series:
    bars: list[DailyBar]
    index: dict[str, int]           # date → bars 내 위치
    closes: list[float]
    ma_fast: list[Optional[float]]
    ma_slow: list[Optional[float]]
    rsi: list[Optional[float]]
    atr: list[Optional[float]]
    avg_value: list[Optional[float]]  # 20일 평균 거래대금


class SwingBacktester:
    """일봉 스윙 백테스터. 신호=종가 확정, 체결=익일 시가."""

    def __init__(self, bars_by_symbol: dict[str, list[DailyBar]],
                 swing: SwingConfig, base_config: dict):
        self.swing = swing
        cost = (base_config or {}).get("cost", {}) or {}
        self.commission_pct = float(cost.get("commission_pct", 0.015))
        self.tax_pct = float(cost.get("tax_pct", 0.20))
        execution = (base_config or {}).get("execution", {}) or {}
        self.slippage_bps = int(execution.get("slippage_bps", 5))

        self.series: dict[str, _Series] = {}
        for symbol, bars in bars_by_symbol.items():
            if not bars:
                continue
            closes = [b.close for b in bars]
            values = [b.value_krw for b in bars]
            self.series[symbol] = _Series(
                bars=bars,
                index={b.date: i for i, b in enumerate(bars)},
                closes=closes,
                ma_fast=sma(closes, swing.ma_fast),
                ma_slow=sma(closes, swing.ma_slow),
                rsi=rsi_series(closes, swing.rsi_period),
                atr=atr_series(bars, swing.atr_period),
                avg_value=sma(values, 20),
            )
        self.all_dates = sorted({b.date for bars in bars_by_symbol.values() for b in bars})

    # ── 체결가/비용 (기존 백테스트와 동일 모델) ──
    def _buy_fill(self, price: float) -> float:
        return price * (1 + self.slippage_bps / 10000)

    def _sell_fill(self, price: float) -> float:
        return price * (1 - self.slippage_bps / 10000)

    def _buy_cost(self, fill: float, qty: int) -> float:
        return fill * qty * self.commission_pct / 100

    def _sell_cost(self, fill: float, qty: int) -> float:
        return fill * qty * (self.commission_pct + self.tax_pct) / 100

    def _regime_ok(self, date: str) -> bool:
        s = self.series.get(self.swing.regime_symbol)
        if s is None:
            return True    # 레짐 데이터가 없으면 필터 비활성
        i = s.index.get(date)
        if i is None or s.ma_fast[i] is None:
            return True
        return s.closes[i] > s.ma_fast[i]

    def _entry_signal(self, symbol: str, date: str) -> Optional[dict]:
        """종가 기준 진입 신호. 조건 미충족이면 None."""
        s = self.series[symbol]
        i = s.index.get(date)
        if i is None or i < 1:
            return None
        ma_f, ma_s = s.ma_fast[i], s.ma_slow[i]
        atr = s.atr[i]
        if ma_f is None or ma_s is None or atr is None:
            return None
        close = s.closes[i]
        if not (close > ma_s and ma_f > ma_s):            # 추세
            return None
        if s.avg_value[i] is None or s.avg_value[i] < self.swing.min_avg_value_krw:
            return None                                    # 유동성
        lookback = self.swing.pullback_lookback
        recent_rsi = [s.rsi[j] for j in range(max(0, i - lookback + 1), i + 1)]
        recent_rsi = [r for r in recent_rsi if r is not None]
        if not recent_rsi or min(recent_rsi) > self.swing.rsi_pullback:
            return None                                    # 눌림 없음
        if not (close > s.closes[i - 1]):                   # 반등 확인
            return None
        return {"atr": atr, "rsi": min(recent_rsi), "prev_close": s.closes[i]}

    def run(self, capital: float = 10_000_000,
            window: Optional[tuple[str, str]] = None) -> dict:
        """window=(시작일, 종료일): 진입은 구간 안에서만, 종료 시 전량 청산."""
        sw = self.swing
        dates = self.all_dates
        if window:
            dates = [d for d in dates if window[0] <= d <= window[1]]
        if not dates:
            return {"trades": [], "equity": [], "end_capital": capital}

        date_pos = {d: gi for gi, d in enumerate(self.all_dates)}
        cash = capital
        positions: dict[str, _Position] = {}
        pending_exit: dict[str, str] = {}   # 종목 → 청산 사유 (다음날 시가 실행)
        trades: list[BTTrade] = []
        equity: list[tuple[str, float]] = []

        def close_position(pos: _Position, date: str, raw_price: float, reason: str) -> None:
            nonlocal cash
            fill = self._sell_fill(raw_price)
            sell_cost = self._sell_cost(fill, pos.qty)
            gross = (fill - pos.entry_price) * pos.qty
            cost = pos.entry_cost + sell_cost
            cash += fill * pos.qty - sell_cost
            trades.append(BTTrade(
                symbol=pos.symbol, entry_time=pos.entry_date, exit_time=date,
                entry_price=pos.entry_price, exit_price=fill, qty=pos.qty,
                reason=reason, gross=gross, cost=cost,
                date=date, r_unit_pct=pos.r_unit_pct,
            ))
            del positions[pos.symbol]
            pending_exit.pop(pos.symbol, None)

        for di, date in enumerate(dates):
            gi = date_pos[date]
            is_last = di == len(dates) - 1

            # 1) 예약 청산 (추세이탈/시간 — 전일 종가 판정)을 시가에 실행
            for symbol, reason in list(pending_exit.items()):
                if symbol not in positions:
                    pending_exit.pop(symbol, None)
                    continue
                s = self.series[symbol]
                j = s.index.get(date)
                if j is not None:
                    close_position(positions[symbol], date, s.bars[j].open, reason)

            # 2) 손절 (장중) — 갭하락이면 시가, 아니면 손절가
            for symbol, pos in list(positions.items()):
                s = self.series[symbol]
                j = s.index.get(date)
                if j is None:
                    continue
                bar = s.bars[j]
                if bar.open <= pos.stop_price:
                    close_position(pos, date, bar.open, "손절")
                elif bar.low <= pos.stop_price:
                    close_position(pos, date, pos.stop_price, "손절")

            # 3) 신규 진입 (전일 종가 신호 → 오늘 시가)
            if di > 0 and not is_last:
                prev_date = dates[di - 1]
                if len(positions) < sw.max_positions and self._regime_ok(prev_date):
                    equity_now = cash
                    for p in positions.values():
                        ps = self.series[p.symbol]
                        pj = ps.index.get(prev_date)
                        equity_now += p.qty * (ps.closes[pj] if pj is not None else p.entry_price)
                    signals = []
                    for symbol in self.series:
                        if symbol == sw.regime_symbol or symbol in positions:
                            continue
                        sig = self._entry_signal(symbol, prev_date)
                        if sig:
                            signals.append((sig["rsi"], symbol, sig))
                    signals.sort(key=lambda x: x[0])   # 더 깊이 눌린 순
                    entered = 0
                    for _rsi, symbol, sig in signals:
                        if entered >= sw.max_entries_per_day:
                            break
                        if len(positions) >= sw.max_positions:
                            break
                        s = self.series[symbol]
                        j = s.index.get(date)
                        if j is None:
                            continue
                        bar = s.bars[j]
                        if bar.open > sig["prev_close"] * (1 + sw.gap_up_limit_pct / 100):
                            continue                    # 갭상승 추격 금지
                        if bar.open < sig["prev_close"] * (1 - sw.gap_down_limit_pct / 100):
                            continue                    # 갭하락: 밤사이 정보 충격 — 신호 무효
                        fill = self._buy_fill(bar.open)
                        stop = bar.open - sw.stop_atr_mult * sig["atr"]
                        risk_per_share = fill - stop
                        if risk_per_share <= 0:
                            continue
                        risk_budget = equity_now * sw.risk_pct / 100
                        qty = int(risk_budget // risk_per_share)
                        cap_by_weight = int((equity_now * sw.max_position_pct / 100) // fill)
                        qty = min(qty, cap_by_weight)
                        qty = min(qty, int(cash // fill))
                        if qty <= 0:
                            continue
                        buy_cost = self._buy_cost(fill, qty)
                        cash -= fill * qty + buy_cost
                        positions[symbol] = _Position(
                            symbol=symbol, entry_date=date, entry_price=fill, qty=qty,
                            stop_price=stop,
                            r_unit_pct=(fill - stop) / fill * 100,
                            entry_cost=buy_cost, entry_index=gi,
                        )
                        entered += 1

            # 4) 종가 판정: 추세이탈/시간 → 내일 시가 청산 예약. 마지막 날은 종가 강제청산
            for symbol, pos in list(positions.items()):
                s = self.series[symbol]
                j = s.index.get(date)
                if j is None:
                    continue
                if is_last:
                    close_position(pos, date, s.bars[j].close, "기간종료")
                    continue
                held = gi - pos.entry_index
                # 추세 판정은 진입과 같은 60일선으로 통일한다. 20일선으로 청산하면
                # 깊은 눌림 뒤 진입하자마자 20일선 아래라 즉시 청산되는 모순이 생긴다.
                trend_break = (s.ma_slow[j] is not None and s.closes[j] < s.ma_slow[j])
                if trend_break:
                    pending_exit[symbol] = "추세이탈"
                elif held >= sw.max_hold_days:
                    pending_exit[symbol] = "시간청산"

            # 5) 평가금액 기록 (종가 기준)
            total = cash
            for p in positions.values():
                ps = self.series[p.symbol]
                pj = ps.index.get(date)
                if pj is not None:
                    total += p.qty * ps.closes[pj]
            equity.append((date, total))

        return {"trades": trades, "equity": equity, "end_capital": cash}


# ──────────────────────────────────────────────
#  워크포워드 검증 (지표·판정은 validation.py 재사용)
# ──────────────────────────────────────────────

def swing_walk_forward(bars_by_symbol: dict[str, list[DailyBar]], swing,
                       base_config: dict, capital: float = 10_000_000,
                       n_folds: int = 4, warmup_days: int = 60,
                       fallback_r_pct: float = 3.0,
                       backtester_cls=None) -> dict:
    """폴드별 파라미터 최적화는 하지 않는다 (표본이 적어 과적합 위험).

    각 폴드는 독립 자본으로 시작하고, 지표는 폴드 거래를 합친 OOS 기준으로 계산한다.
    지표 계산에 필요한 구간 앞부분(warmup_days)은 신호 warmup 전용으로 쓴다.
    backtester_cls를 주면 다른 일봉 전략(예: 돌파형)도 같은 틀로 검증할 수 있다.
    """
    from .validation import (compute_metrics, max_drawdown_pct_from_equity,
                              split_folds)

    bt = (backtester_cls or SwingBacktester)(bars_by_symbol, swing, base_config)
    all_dates = bt.all_dates
    eligible = all_dates[warmup_days:]
    if len(eligible) < n_folds * 5:
        return {"folds": [], "oos_metrics": compute_metrics([], fallback_r_pct),
                "dates": all_dates, "stitched_equity": [],
                "note": f"데이터 부족: warmup {warmup_days}일 이후 {len(eligible)}일뿐입니다"}

    fold_dates_list = split_folds(eligible, n_folds)
    folds = []
    all_trades: list[BTTrade] = []
    stitched: list[float] = []
    positive = 0
    for i, fdates in enumerate(fold_dates_list):
        result = bt.run(capital=capital, window=(fdates[0], fdates[-1]))
        trades = sorted(result["trades"], key=lambda t: (t.date, t.symbol))
        metrics = compute_metrics(trades, fallback_r_pct)
        folds.append({
            "fold": i + 1, "start": fdates[0], "end": fdates[-1],
            "trades": trades, "metrics": metrics,
            "end_capital": result["end_capital"],
        })
        all_trades.extend(trades)
        if metrics["net_pnl"] > 0:
            positive += 1
        curve = [eq for _d, eq in result["equity"]]
        if curve:
            if stitched:
                offset = stitched[-1] - curve[0]
                stitched.extend(v + offset for v in curve[1:])
            else:
                stitched.extend(curve)

    all_trades.sort(key=lambda t: (t.date, t.symbol))
    oos = compute_metrics(all_trades, fallback_r_pct)
    oos["positive_fold_ratio"] = positive / len(folds) if folds else 0.0
    oos["max_drawdown_pct"] = max_drawdown_pct_from_equity(stitched) if stitched else 0.0
    oos["final_capital"] = capital + oos["net_pnl"]
    return {"folds": folds, "oos_metrics": oos, "dates": all_dates,
            "stitched_equity": stitched, "note": ""}


def render_swing_report(result: dict, v: dict, data_dir: str = "",
                        regime_active: bool = True, capital: float = 10_000_000,
                        title: str = "일봉 스윙 전략 검증 리포트") -> str:
    m = result["oos_metrics"]
    lines = [
        f"# {title}",
        "",
        f"- 데이터: {data_dir} — {result['dates'][0] if result['dates'] else '-'} ~ "
        f"{result['dates'][-1] if result['dates'] else '-'}",
        f"- 방식: 워크포워드 {len(result['folds'])}폴드 (폴드별 최적화 없음, 고정 파라미터)",
        f"- 레짐 필터: {'적용 (069500 20일선)' if regime_active else '미적용 (레짐 데이터 없음)'}",
        f"- 자본: {capital:,.0f}원 → {m.get('final_capital', capital):,.0f}원 "
        f"({(m.get('final_capital', capital) / capital - 1) * 100:+.2f}%)",
    ]
    if result.get("note"):
        lines.append(f"- 참고: {result['note']}")
    lines += ["", f"## 판정: {v['status']}", "",
              "| 게이트 | 결과 | 판정 |", "|---|---|---|"]
    for c in v["checks"]:
        lines.append(f"| {c['name']} | {c['display']} | {'통과' if c['ok'] else '미달'} |")
    lines += [
        "", "## 아웃오브샘플 지표", "",
        f"- 거래 {m['trades']}회 · 승률 {m['win_rate']:.1f}% · PF {m['profit_factor']:.2f}",
        f"- 기대값 {m['expectancy_r']:+.3f}R/거래 ({m['expectancy_krw']:+,.0f}원/거래)",
        f"- 평균 이익 {m['avg_win_r']:+.2f}R · 평균 손실 {m['avg_loss_r']:+.2f}R · "
        f"최대 연속손실 {m['max_consecutive_losses']}회",
        f"- 최대 낙폭 {m['max_drawdown_pct']:.2f}% · 총 비용 {m['total_cost']:,.0f}원",
        "", "## 폴드별 결과", "",
        "| 폴드 | 구간 | 거래 | 순손익 | 기대값(R) |", "|---|---|---|---|---|",
    ]
    for f in result["folds"]:
        fm = f["metrics"]
        lines.append(f"| {f['fold']} | {f['start']}~{f['end']} | {fm['trades']} | "
                     f"{fm['net_pnl']:+,.0f}원 | {fm['expectancy_r']:+.3f} |")
    lines += ["", "## 청산 사유별 (OOS)", "",
              "| 사유 | 횟수 | 순손익 | 평균 R |", "|---|---|---|---|"]
    for reason, b in sorted(m["exit_reasons"].items(), key=lambda kv: kv[1]["count"], reverse=True):
        lines.append(f"| {reason} | {b['count']} | {b['net']:+,.0f}원 | {b['avg_r']:+.2f} |")
    lines += ["", "## 월별 (OOS)", "", "| 월 | 거래 | 순손익 |", "|---|---|---|"]
    for month, b in sorted(m["monthly"].items()):
        lines.append(f"| {month} | {b['count']} | {b['net']:+,.0f}원 |")
    lines += [
        "", "## 해석 시 주의", "",
        "- 백테스트 유니버스는 수집된 종목 집합이라 생존·선정 편향이 있다.",
        "- 손절은 갭하락 시 시가 체결로 반영했지만, 실제 체결은 이보다 나쁠 수 있다.",
        "- 일봉만으로는 장중 체결 순서를 알 수 없어 손절·진입이 같은 날 겹치면 보수적으로 처리했다.",
        "- PASS라도 실전 투입은 모의(paper) 검증 후 소액부터 단계적으로 할 것.",
    ]
    return "\n".join(lines) + "\n"
