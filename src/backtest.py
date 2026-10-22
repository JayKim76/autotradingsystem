"""
backtest.py — 오프라인 백테스트 엔진

실계좌·모의계좌 없이 과거 분봉 데이터로 전략을 검증한다.
전략 로직(StrategyEngine)과 리스크 로직(RiskManager)을 실운용과
동일하게 사용하므로, 코드 변경이 백테스트에 그대로 반영된다.

사용::

    from src.backtest import Backtester, load_candles_csv
    bt = Backtester(config, initial_capital=10_000_000)
    bt.run({"005930": load_candles_csv("data/005930.csv")})
    print(bt.report())

CSV 포맷: time,open,high,low,close,volume  (time = 'HHMMSS' 또는 'HHMM')
"""
from __future__ import annotations

import csv
import logging
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .indicators import Candle, SymbolIndicators
from .risk_manager import RiskManager, Position
from .strategy import StrategyEngine, SignalType, ScaleInSignal

logger = logging.getLogger("autotrader.backtest")


@dataclass
class BTTrade:
    """백테스트 체결 기록"""
    symbol:      str
    entry_time:  str
    exit_time:   str
    entry_price: float
    exit_price:  float
    qty:         int
    reason:      str
    gross:       float
    cost:        float
    score:       int = 0

    @property
    def net(self) -> float:
        return self.gross - self.cost

    @property
    def pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price * 100


@dataclass
class BTResult:
    """백테스트 집계 결과"""
    trades:        list[BTTrade] = field(default_factory=list)
    equity_curve:  list[float]   = field(default_factory=list)
    initial:       float         = 0.0
    final:         float         = 0.0

    # ── 성과 지표 ─────────────────────────────────────────────
    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> list[BTTrade]:
        return [t for t in self.trades if t.net > 0]

    @property
    def losses(self) -> list[BTTrade]:
        return [t for t in self.trades if t.net <= 0]

    @property
    def win_rate(self) -> float:
        return len(self.wins) / self.total_trades * 100 if self.total_trades else 0.0

    @property
    def net_pnl(self) -> float:
        return sum(t.net for t in self.trades)

    @property
    def total_cost(self) -> float:
        return sum(t.cost for t in self.trades)

    @property
    def avg_win(self) -> float:
        return statistics.mean([t.net for t in self.wins]) if self.wins else 0.0

    @property
    def avg_loss(self) -> float:
        return statistics.mean([t.net for t in self.losses]) if self.losses else 0.0

    @property
    def profit_factor(self) -> float:
        """총이익 / 총손실 — 1.5 이상이면 양호"""
        gain = sum(t.net for t in self.wins)
        loss = abs(sum(t.net for t in self.losses))
        return gain / loss if loss > 0 else float("inf") if gain > 0 else 0.0

    @property
    def payoff_ratio(self) -> float:
        """평균이익 / 평균손실 (손익비)"""
        return abs(self.avg_win / self.avg_loss) if self.avg_loss else 0.0

    @property
    def expectancy(self) -> float:
        """기대값 (1거래당 평균 순손익)"""
        return self.net_pnl / self.total_trades if self.total_trades else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        """최대 낙폭 (%)"""
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        mdd = 0.0
        for v in self.equity_curve:
            peak = max(peak, v)
            if peak > 0:
                mdd = max(mdd, (peak - v) / peak * 100)
        return mdd

    @property
    def return_pct(self) -> float:
        if self.initial <= 0:
            return 0.0
        return (self.final - self.initial) / self.initial * 100


class Backtester:
    """분봉 리플레이 백테스터"""

    def __init__(self, config: dict, initial_capital: float = 10_000_000) -> None:
        self.config   = config
        self.strategy = StrategyEngine(config)
        self.risk     = RiskManager(config, initial_capital)
        self.initial  = initial_capital

        strat = config.get("strategy", {})
        orb   = config.get("orb", {})
        self.orb_start   = orb.get("start_time", "09:00").replace(":", "")
        self.orb_end     = orb.get("end_time",   "09:30").replace(":", "")
        self.trade_start = strat.get("trading_start", "09:30").replace(":", "")
        self.trade_end   = strat.get("trading_end",   "11:00").replace(":", "")
        self.force_close = strat.get("force_close",   "15:15").replace(":", "")
        self.ma_short    = int(strat.get("ma_short", 5))
        self.ma_long     = int(strat.get("ma_long", 20))

        cost = config.get("cost", {})
        self.commission_pct = float(cost.get("commission_pct", 0.015))
        self.tax_pct        = float(cost.get("tax_pct", 0.18))
        self.slippage_bps   = int(config.get("execution", {}).get("slippage_bps", 5))

        self.result = BTResult(initial=initial_capital)
        self._open: dict[str, tuple[Position, str, int]] = {}   # sym → (pos, entry_time, score)

    # ── 실행 ──────────────────────────────────────────────────
    def run(self, data: dict[str, list[Candle]]) -> BTResult:
        """여러 종목의 분봉을 시간순으로 병합 리플레이

        Args:
            data: {종목코드: [Candle, ...]}
        """
        inds: dict[str, SymbolIndicators] = {
            sym: SymbolIndicators(
                sym, self.ma_short, self.ma_long,
                self.orb_start, self.orb_end,
            )
            for sym in data
        }

        # (시각, 종목, 봉) 튜플로 병합 후 시간순 정렬
        events: list[tuple[str, str, Candle]] = [
            (c.time, sym, c) for sym, candles in data.items() for c in candles
        ]
        events.sort(key=lambda e: (e[0], e[1]))

        orb_finalized: set[str] = set()

        for tstr, sym, candle in events:
            hhmm = candle.hhmm
            ind = inds[sym]
            ind.add_candle(candle)

            # ORB 확정
            if hhmm >= self.orb_end and sym not in orb_finalized:
                ind.set_orb_ready()
                orb_finalized.add(sym)

            price = candle.close

            # ── 보유 포지션 청산 판정 ──────────────────────
            if sym in self._open:
                pos, _, _ = self._open[sym]
                pos.update_peak(price)

                if hhmm >= self.force_close:
                    self._close(sym, price, tstr, "강제청산", SignalType.EXIT_FORCE)
                else:
                    # 청산 판정 입력 조립은 StrategyEngine.evaluate_exit() 공통 사용
                    # (실전 Executor와 동일 로직 — 드리프트 방지).
                    # 경과 시간은 봉 시각 기준 (진입 3분 후부터 VWAP 이탈 판정).
                    try:
                        e_min = int(pos.entry_time[:2]) * 60 + int(pos.entry_time[2:4])
                        c_min = int(tstr[:2]) * 60 + int(tstr[2:4])
                        elapsed_min = float(c_min - e_min)
                    except Exception:
                        elapsed_min = 999.0

                    signal, reason = self.strategy.evaluate_exit(
                        pos, ind, price, elapsed_min=elapsed_min,
                    )
                    if signal == SignalType.ARM_TP3:
                        pos.tp3_armed = True
                    elif signal != SignalType.NONE:
                        self._close(sym, price, tstr, reason, signal)
                    else:
                        # scale-in (물타기 / 피라미딩) 판정
                        if self.risk.can_add_to_position(sym):
                            r_unit = pos.r_unit_pct if pos.r_unit_pct > 0 else (pos.stop_pct or self.strategy.sl_pct)
                            add_sig, size_ratio, add_reason = self.strategy.should_add(
                                indicators=ind,
                                current_price=price,
                                entry_price=pos.entry_price,
                                r_unit_pct=r_unit,
                                add_count=pos.add_count,
                                avg_down_count=pos.avg_down_count,
                                ask_total=0,
                                bid_total=0,
                            )
                            if add_sig != ScaleInSignal.NONE:
                                is_avg_down = (add_sig == ScaleInSignal.AVG_DOWN)
                                add_qty = int(pos.initial_qty * size_ratio)
                                if add_qty > 0:
                                    fill = price * (1 + self.slippage_bps / 10000)
                                    cost = fill * add_qty * self.commission_pct / 100
                                    if self.risk.capital >= fill * add_qty + cost:
                                        new_stop_pct = 0.0
                                        new_r_unit_pct = 0.0
                                        if self.risk.scale_in_recalc_stop:
                                            new_stop_pct = self.strategy.resolve_stop_pct(ind.atr_pct)
                                            new_r_unit_pct = self.strategy.resolve_r_unit_pct(new_stop_pct)
                                        self.risk.add_to_position(
                                            sym, fill, add_qty, cost=cost,
                                            is_avg_down=is_avg_down,
                                            new_stop_pct=new_stop_pct,
                                            new_r_unit_pct=new_r_unit_pct
                                        )

            # ── 진입 판정 ──────────────────────────────────
            elif self.trade_start <= hhmm < self.trade_end:
                if not self.risk.can_enter(sym):
                    continue
                # 호가 데이터가 없으므로 매수우세 항목은 미충족 처리
                ok, score = self.strategy.should_enter(ind, price, 0, 0)
                if ok:
                    self._open_position(sym, price, tstr, score.score, ind)

            self.result.equity_curve.append(self.risk.capital)

        # 미청산 포지션 정리
        for sym in list(self._open.keys()):
            pos, _, _ = self._open[sym]
            self._close(sym, pos.peak_price, "153000", "종료청산", SignalType.EXIT_FORCE)

        self.result.final = self.risk.capital
        return self.result

    # ── 내부 ──────────────────────────────────────────────────
    def _open_position(self, sym: str, price: float, tstr: str, score: int, ind: SymbolIndicators) -> None:
        fill = price * (1 + self.slippage_bps / 10000)
        atr_pct = ind.atr_pct if ind is not None else None
        stop_pct = self.strategy.resolve_stop_pct(atr_pct)
        r_unit_pct = self.strategy.resolve_r_unit_pct(stop_pct)

        qty  = self.risk.calc_position_size(fill, stop_pct=stop_pct)
        if qty <= 0:
            return
        cost = fill * qty * self.commission_pct / 100
        pos = Position(
            symbol=sym, entry_price=fill, qty=qty,
            entry_time=tstr, score=score,
            stop_pct=stop_pct, r_unit_pct=r_unit_pct,
        )
        self.risk.open_position(pos, cost=cost)
        self._open[sym] = (pos, tstr, score)

    def _close(self, sym: str, price: float, tstr: str,
               reason: str, signal: SignalType) -> None:
        entry = self._open.get(sym)
        if not entry:
            return
        pos, entry_time, score = entry

        qty = self.strategy.exit_qty(pos.qty, pos.initial_qty, signal)
        if qty <= 0:
            return

        fill  = price * (1 - self.slippage_bps / 10000)
        cost  = fill * qty * (self.commission_pct + self.tax_pct) / 100
        gross = (fill - pos.entry_price) * qty
        entry_price = pos.entry_price

        self.risk.close_position(sym, fill, qty, cost=cost)

        if signal == SignalType.EXIT_TP1:
            pos.tp1_done = True
        elif signal == SignalType.EXIT_TP2:
            pos.tp2_done = True

        self.result.trades.append(BTTrade(
            symbol=sym, entry_time=entry_time, exit_time=tstr,
            entry_price=entry_price, exit_price=fill, qty=qty,
            reason=reason, gross=gross, cost=cost, score=score,
        ))

        if sym not in self.risk.positions:
            del self._open[sym]

    # ── 리포트 ────────────────────────────────────────────────
    def report(self) -> str:
        r = self.result
        lines = [
            "═" * 52,
            "  백테스트 결과",
            "═" * 52,
            f"  초기 자본     : {r.initial:>15,.0f} 원",
            f"  최종 자본     : {r.final:>15,.0f} 원",
            f"  총 수익률     : {r.return_pct:>15.2f} %",
            f"  순손익        : {r.net_pnl:>15,.0f} 원",
            f"  총 비용       : {r.total_cost:>15,.0f} 원",
            "─" * 52,
            f"  총 거래       : {r.total_trades:>15} 회",
            f"  승 / 패       : {len(r.wins):>7} / {len(r.losses):<7}",
            f"  승률          : {r.win_rate:>15.1f} %",
            f"  평균 이익     : {r.avg_win:>15,.0f} 원",
            f"  평균 손실     : {r.avg_loss:>15,.0f} 원",
            f"  손익비        : {r.payoff_ratio:>15.2f}",
            f"  Profit Factor : {r.profit_factor:>15.2f}",
            f"  기대값/거래   : {r.expectancy:>15,.0f} 원",
            f"  최대 낙폭     : {r.max_drawdown_pct:>15.2f} %",
            "═" * 52,
        ]

        if r.trades:
            lines.append("  청산 사유별 분포")
            reasons: dict[str, list[BTTrade]] = {}
            for t in r.trades:
                key = t.reason.split("(")[0]
                reasons.setdefault(key, []).append(t)
            for k, ts in sorted(reasons.items(), key=lambda x: -len(x[1])):
                total = sum(t.net for t in ts)
                lines.append(f"    {k:<14} {len(ts):>3}회   {total:>12,.0f}원")
            lines.append("═" * 52)

        return "\n".join(lines)


# ──────────────────────────────────────────────
#  데이터 로더
# ──────────────────────────────────────────────
def load_candles_csv(path: str | Path) -> list[Candle]:
    """CSV → Candle 리스트

    필요 컬럼: time, open, high, low, close, volume
    """
    candles: list[Candle] = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                t = str(row["time"]).replace(":", "").ljust(6, "0")
                candles.append(Candle(
                    time=t,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(float(row["volume"])),
                ))
            except (KeyError, ValueError) as e:
                logger.warning("CSV 행 파싱 실패: %s | %s", e, row)
    candles.sort(key=lambda c: c.time)
    return candles


def fetch_candles_from_api(client, symbol: str) -> list[Candle]:
    """KIS API에서 당일 분봉을 받아 Candle 리스트로 변환 (당일 검증용)"""
    from .indicators import parse_candles
    rows = client.get_minute_candles_full(symbol)
    return parse_candles(rows, exclude_forming=False)


# ──────────────────────────────────────────────
#  다일자 백테스트 (실데이터용)
# ──────────────────────────────────────────────
@dataclass
class DayResult:
    """하루치 백테스트 결과"""
    date:      str
    trades:    int
    net_pnl:   float
    capital:   float
    win:       int
    loss:      int


class MultiDayBacktester:
    """여러 영업일에 걸친 백테스트

    실운용과 동일하게 **매일 리스크 상태를 초기화**하고 자본은 이어받는다.
    이렇게 해야 일일 손실 한도·연속 손실 차단이 실제와 같이 작동한다.
    """

    def __init__(self, config: dict, initial_capital: float = 10_000_000) -> None:
        self.config  = config
        self.capital = initial_capital
        self.initial = initial_capital
        self.days: list[DayResult] = []
        self.all_trades: list[BTTrade] = []
        self.equity: list[float] = [initial_capital]

    def run(self, data_by_date: dict[str, dict[str, list[Candle]]]) -> "MultiDayBacktester":
        """
        Args:
            data_by_date: {날짜: {종목: [Candle, ...]}}
        """
        for date_str in sorted(data_by_date):
            day_data = data_by_date[date_str]
            if not day_data:
                continue

            bt = Backtester(self.config, initial_capital=self.capital)
            res = bt.run(day_data)

            day_pnl = res.net_pnl
            self.capital = bt.risk.capital

            self.days.append(DayResult(
                date=date_str,
                trades=len(res.trades),
                net_pnl=day_pnl,
                capital=self.capital,
                win=len(res.wins),
                loss=len(res.losses),
            ))
            self.all_trades.extend(res.trades)
            self.equity.append(self.capital)

        return self

    # ── 집계 ──────────────────────────────────────────────────
    @property
    def total_pnl(self) -> float:
        return self.capital - self.initial

    @property
    def return_pct(self) -> float:
        return self.total_pnl / self.initial * 100 if self.initial else 0.0

    @property
    def win_days(self) -> int:
        return sum(1 for d in self.days if d.net_pnl > 0)

    @property
    def max_drawdown_pct(self) -> float:
        peak, mdd = self.equity[0], 0.0
        for v in self.equity:
            peak = max(peak, v)
            if peak > 0:
                mdd = max(mdd, (peak - v) / peak * 100)
        return mdd

    @property
    def trade_win_rate(self) -> float:
        wins = sum(1 for t in self.all_trades if t.net > 0)
        return wins / len(self.all_trades) * 100 if self.all_trades else 0.0

    @property
    def profit_factor(self) -> float:
        gain = sum(t.net for t in self.all_trades if t.net > 0)
        loss = abs(sum(t.net for t in self.all_trades if t.net <= 0))
        return gain / loss if loss > 0 else float("inf") if gain > 0 else 0.0

    @property
    def sharpe(self) -> float:
        """일별 수익률 기준 샤프 비율 (연율화, 무위험수익률 0 가정)"""
        if len(self.days) < 2:
            return 0.0
        rets = []
        prev = self.initial
        for d in self.days:
            if prev > 0:
                rets.append((d.capital - prev) / prev)
            prev = d.capital
        if len(rets) < 2:
            return 0.0
        mean = statistics.mean(rets)
        sd = statistics.stdev(rets)
        return (mean / sd * (252 ** 0.5)) if sd > 0 else 0.0

    def report(self) -> str:
        W = 56
        if not self.days:
            return "백테스트할 데이터가 없습니다."

        lines = [
            "═" * W,
            "  다일자 백테스트 결과",
            "═" * W,
            f"  기간          : {self.days[0].date} ~ {self.days[-1].date}",
            f"  영업일        : {len(self.days)}일",
            "─" * W,
            f"  초기 자본     : {self.initial:>15,.0f} 원",
            f"  최종 자본     : {self.capital:>15,.0f} 원",
            f"  총 손익       : {self.total_pnl:>15,.0f} 원",
            f"  총 수익률     : {self.return_pct:>15.2f} %",
            "─" * W,
            f"  총 거래       : {len(self.all_trades):>15} 회",
            f"  거래 승률     : {self.trade_win_rate:>15.1f} %",
            f"  수익일/전체   : {self.win_days:>7} / {len(self.days):<7}",
            f"  일 승률       : {self.win_days / len(self.days) * 100:>15.1f} %",
            f"  Profit Factor : {self.profit_factor:>15.2f}",
            f"  최대 낙폭     : {self.max_drawdown_pct:>15.2f} %",
            f"  샤프 비율     : {self.sharpe:>15.2f}",
            "═" * W,
        ]

        # 최근 10일 상세
        lines.append("  일자별 (최근 10일)")
        lines.append(f"  {'날짜':<10}{'거래':>5}{'승/패':>8}{'손익':>13}{'자본':>15}")
        for d in self.days[-10:]:
            lines.append(
                f"  {d.date:<10}{d.trades:>5}"
                f"{f'{d.win}/{d.loss}':>8}"
                f"{d.net_pnl:>13,.0f}{d.capital:>15,.0f}"
            )
        lines.append("═" * W)

        # 판정 가이드
        lines.append("  판정 기준")
        pf, wr, mdd = self.profit_factor, self.trade_win_rate, self.max_drawdown_pct
        lines.append(f"    Profit Factor ≥ 1.5  : "
                     f"{'통과' if pf >= 1.5 else '미달'} ({pf:.2f})")
        lines.append(f"    거래 승률 ≥ 45%      : "
                     f"{'통과' if wr >= 45 else '미달'} ({wr:.1f}%)")
        lines.append(f"    최대 낙폭 ≤ 15%      : "
                     f"{'통과' if mdd <= 15 else '미달'} ({mdd:.1f}%)")
        lines.append(f"    표본 ≥ 60영업일       : "
                     f"{'통과' if len(self.days) >= 60 else '미달'} ({len(self.days)}일)")
        lines.append("═" * W)

        return "\n".join(lines)
