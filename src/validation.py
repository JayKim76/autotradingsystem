"""
validation.py — 실데이터 백테스트 검증 체계

단일 백테스트 숫자(총수익률)는 과적합과 구간 운에 취약하다.
이 모듈은 다음을 강제한다:

  1. 워크포워드: 데이터를 시간순 폴드로 나누고, (선택 시) 각 폴드의
     '이전 구간'에서만 파라미터를 고른 뒤 '다음 구간(아웃오브샘플)'으로 평가한다.
  2. R 단위 지표: 손익을 원화가 아니라 진입 시 확정된 1R 기준으로 환산해
     자본 규모와 무관하게 전략 자체의 기대값을 본다.
  3. 판정 게이트: 기대값·PF·폴드 일관성·표본 수·낙폭을 기준으로
     PASS/FAIL을 기계적으로 판정한다 (느낌으로 "좋아 보인다" 금지).

데이터는 src/collector.py 포맷(data/candles/YYYYMMDD/종목.csv)을 쓴다.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Optional

from .backtest import BTTrade, MultiDayBacktester
from .config_loader import deep_merge
from .strategy import StrategyEngine

logger = logging.getLogger("autotrader.validation")

# ──────────────────────────────────────────────
#  기본 판정 게이트 (CLI/설정으로 조정 가능)
# ──────────────────────────────────────────────
DEFAULT_GATES = {
    "min_oos_trades": 30,        # 아웃오브샘플 최소 거래 수 (표본 부족이면 판정 불가)
    "min_expectancy_r": 0.05,    # 거래당 기대값 하한 (R)
    "min_profit_factor": 1.2,    # PF 하한
    "min_positive_fold_ratio": 0.6,  # 수익 폴드 비율 하한
    "max_drawdown_pct": 15.0,    # 아웃오브샘플 최대 낙폭 상한 (%)
}

# 워크포워드 최적화 후보 (작게 유지 — 후보가 많을수록 과적합)
DEFAULT_GRID: list[dict] = [
    {},  # 기준 설정
    {"strategy": {"min_score": 4}},
    {"strategy": {"min_score": 5}, "take_profit": {"tp1_r": 1.2}},
    {"strategy": {"min_score": 4}, "take_profit": {"tp1_r": 1.2}},
    {"strategy": {"min_score": 5}, "take_profit": {"tp1_r": 2.0, "tp2_r": 3.5}},
]


def _combo_name(override: dict) -> str:
    if not override:
        return "baseline"
    parts = []
    for sec, fields in sorted(override.items()):
        for k, v in sorted(fields.items()):
            parts.append(f"{k}={v}")
    return ", ".join(parts)


# ──────────────────────────────────────────────
#  지표 계산
# ──────────────────────────────────────────────
def default_r_pct(config: dict) -> float:
    """BTTrade에 r_unit_pct가 없을 때 쓸 기본 1R 크기 (%)"""
    engine = StrategyEngine(config)
    return engine.resolve_r_unit_pct(engine.resolve_stop_pct(None))


def trade_r(trade: BTTrade, fallback_r_pct: float) -> float:
    """거래 손익을 R 배수로 환산. 리스크액 = 진입금액 × 1R%"""
    r_pct = trade.r_unit_pct if trade.r_unit_pct and trade.r_unit_pct > 0 else fallback_r_pct
    risk_amount = trade.entry_price * trade.qty * r_pct / 100.0
    if risk_amount <= 0:
        return 0.0
    return trade.net / risk_amount


def compute_metrics(trades: list[BTTrade], fallback_r_pct: float) -> dict:
    """거래 목록 → 검증 지표 dict"""
    rs = [trade_r(t, fallback_r_pct) for t in trades]
    wins = [t for t in trades if t.net > 0]
    losses = [t for t in trades if t.net <= 0]
    gain = sum(t.net for t in wins)
    loss = abs(sum(t.net for t in losses))
    total_cost = sum(t.cost for t in trades)

    # 최대 연속 손실 (시간순)
    max_streak, streak = 0, 0
    for t in trades:
        if t.net <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    # 청산 사유별 분포
    reasons: dict[str, dict] = {}
    for t, r in zip(trades, rs):
        key = (t.reason or "기타").split("(")[0].strip() or "기타"
        bucket = reasons.setdefault(key, {"count": 0, "net": 0.0, "r_sum": 0.0})
        bucket["count"] += 1
        bucket["net"] += t.net
        bucket["r_sum"] += r
    for bucket in reasons.values():
        bucket["avg_r"] = bucket["r_sum"] / bucket["count"] if bucket["count"] else 0.0
        del bucket["r_sum"]

    # 월별 분포 (YYYYMM)
    monthly: dict[str, dict] = {}
    for t in trades:
        if t.date and len(t.date) >= 6:
            m = monthly.setdefault(t.date[:6], {"count": 0, "net": 0.0})
            m["count"] += 1
            m["net"] += t.net

    n = len(trades)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / n * 100) if n else 0.0,
        "net_pnl": sum(t.net for t in trades),
        "profit_factor": (gain / loss) if loss > 0 else (float("inf") if gain > 0 else 0.0),
        "expectancy_krw": (sum(t.net for t in trades) / n) if n else 0.0,
        "expectancy_r": (sum(rs) / n) if n else 0.0,
        "avg_win_r": (sum(r for r, t in zip(rs, trades) if t.net > 0) / len(wins)) if wins else 0.0,
        "avg_loss_r": (sum(r for r, t in zip(rs, trades) if t.net <= 0) / len(losses)) if losses else 0.0,
        "best_r": max(rs) if rs else 0.0,
        "worst_r": min(rs) if rs else 0.0,
        "max_consecutive_losses": max_streak,
        "total_cost": total_cost,
        "cost_per_trade": (total_cost / n) if n else 0.0,
        "exit_reasons": reasons,
        "monthly": monthly,
    }


def max_drawdown_pct_from_equity(equity: list[float]) -> float:
    if not equity:
        return 0.0
    peak, mdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak * 100)
    return mdd


# ──────────────────────────────────────────────
#  워크포워드
# ──────────────────────────────────────────────
def split_folds(dates: list[str], n_folds: int) -> list[list[str]]:
    """날짜를 시간순 연속 폴드로 분할 (마지막 폴드에 나머지 흡수)"""
    dates = sorted(dates)
    if n_folds <= 1 or len(dates) <= 1:
        return [dates] if dates else []
    size = max(1, len(dates) // n_folds)
    folds = [dates[i * size:(i + 1) * size] for i in range(n_folds - 1)]
    folds.append(dates[(n_folds - 1) * size:])
    return [f for f in folds if f]


@dataclass
class FoldResult:
    index: int
    train_dates: list[str] = field(default_factory=list)
    test_dates: list[str] = field(default_factory=list)
    params_name: str = "baseline"
    params: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    return_pct: float = 0.0


def _run_bt(config: dict, data: dict, capital: float) -> MultiDayBacktester:
    mbt = MultiDayBacktester(copy.deepcopy(config), initial_capital=capital)
    mbt.run(data)
    return mbt


def walk_forward(
    data_by_date: dict[str, dict],
    config: dict,
    capital: float = 10_000_000,
    n_folds: int = 4,
    optimize: bool = False,
    grid: Optional[list[dict]] = None,
    min_train_trades: int = 10,
) -> dict:
    """워크포워드 검증 실행

    optimize=False: 전 폴드를 기준 설정으로 평가 (구간 일관성 확인용)
    optimize=True : 폴드 i의 파라미터는 폴드 i 이전 구간(학습)에서만
                    기대값(R) 기준으로 고른다. 첫 폴드는 학습 구간이 없어 기준 설정 사용.
    """
    dates = sorted(data_by_date.keys())
    folds = split_folds(dates, n_folds)
    fallback_r = default_r_pct(config)
    grid = grid if grid is not None else DEFAULT_GRID

    fold_results: list[FoldResult] = []
    oos_trades: list[BTTrade] = []
    oos_equity: list[float] = [capital]
    running_capital = capital

    for i, test_dates in enumerate(folds):
        train_dates = [d for f in folds[:i] for d in f]
        chosen, chosen_name = {}, "baseline"

        if optimize and train_dates:
            train_data = {d: data_by_date[d] for d in train_dates}
            best_score, best = None, None
            for override in grid:
                merged = deep_merge(config, override)
                mbt = _run_bt(merged, train_data, capital)
                m = compute_metrics(mbt.all_trades, default_r_pct(merged))
                if m["trades"] < min_train_trades:
                    continue
                if best_score is None or m["expectancy_r"] > best_score:
                    best_score, best = m["expectancy_r"], override
            if best is not None:
                chosen, chosen_name = best, _combo_name(best)

        merged = deep_merge(config, chosen)
        test_data = {d: data_by_date[d] for d in test_dates}
        mbt = _run_bt(merged, test_data, running_capital)
        metrics = compute_metrics(mbt.all_trades, default_r_pct(merged))
        fold_results.append(FoldResult(
            index=i, train_dates=train_dates, test_dates=test_dates,
            params_name=chosen_name, params=chosen,
            metrics=metrics, return_pct=mbt.return_pct,
        ))
        oos_trades.extend(mbt.all_trades)
        oos_equity.extend(mbt.equity[1:])  # 폴드 시작점 중복 제거
        running_capital = mbt.capital

    oos_trades.sort(key=lambda t: (t.date, t.entry_time))
    oos_metrics = compute_metrics(oos_trades, fallback_r)
    oos_metrics["return_pct"] = (running_capital - capital) / capital * 100 if capital else 0.0
    oos_metrics["max_drawdown_pct"] = max_drawdown_pct_from_equity(oos_equity)
    positive_folds = sum(1 for f in fold_results if f.metrics["net_pnl"] > 0)
    oos_metrics["positive_fold_ratio"] = (
        positive_folds / len(fold_results) if fold_results else 0.0
    )

    return {
        "dates": dates,
        "n_folds": len(folds),
        "optimize": optimize,
        "capital": capital,
        "final_capital": running_capital,
        "folds": fold_results,
        "oos_metrics": oos_metrics,
        "oos_trades": oos_trades,
    }


# ──────────────────────────────────────────────
#  판정 게이트
# ──────────────────────────────────────────────
def verdict(oos_metrics: dict, gates: Optional[dict] = None) -> dict:
    """게이트 판정. 표본 부족이면 PASS가 아니라 INSUFFICIENT(판정 불가)."""
    g = {**DEFAULT_GATES, **(gates or {})}
    checks = []

    def add(name, value, threshold, ok, fmt="{:.2f}"):
        checks.append({
            "name": name, "value": value, "threshold": threshold,
            "ok": bool(ok), "display": f"{fmt.format(value)} (기준 {fmt.format(threshold)})",
        })

    add("OOS 거래 수", oos_metrics["trades"], g["min_oos_trades"],
        oos_metrics["trades"] >= g["min_oos_trades"], fmt="{:.0f}")
    add("기대값 (R/거래)", oos_metrics["expectancy_r"], g["min_expectancy_r"],
        oos_metrics["expectancy_r"] > g["min_expectancy_r"])
    add("Profit Factor", oos_metrics["profit_factor"], g["min_profit_factor"],
        oos_metrics["profit_factor"] >= g["min_profit_factor"])
    add("수익 폴드 비율", oos_metrics["positive_fold_ratio"], g["min_positive_fold_ratio"],
        oos_metrics["positive_fold_ratio"] >= g["min_positive_fold_ratio"])
    add("최대 낙폭 (%)", oos_metrics["max_drawdown_pct"], g["max_drawdown_pct"],
        oos_metrics["max_drawdown_pct"] <= g["max_drawdown_pct"])

    if oos_metrics["trades"] < g["min_oos_trades"]:
        status = "INSUFFICIENT"   # 표본 부족 — 좋고 나쁨을 말할 수 없음
    elif all(c["ok"] for c in checks):
        status = "PASS"
    else:
        status = "FAIL"
    return {"status": status, "checks": checks, "gates": g}


# ──────────────────────────────────────────────
#  리포트
# ──────────────────────────────────────────────
def render_report(result: dict, v: dict, data_dir: str = "") -> str:
    m = result["oos_metrics"]
    dates = result["dates"]
    lines = [
        "# 실데이터 백테스트 검증 리포트",
        "",
        f"- 데이터: {data_dir or 'data/candles'} — {len(dates)}거래일 "
        f"({dates[0] if dates else '-'} ~ {dates[-1] if dates else '-'})",
        f"- 방식: 워크포워드 {result['n_folds']}폴드"
        f"{' (폴드별 파라미터 선택 포함)' if result['optimize'] else ' (기준 설정 고정)'}",
        f"- 자본: {result['capital']:,.0f}원 → {result['final_capital']:,.0f}원 "
        f"({m['return_pct']:+.2f}%)",
        "",
        f"## 판정: {v['status']}",
        "",
        "| 게이트 | 결과 | 판정 |",
        "|---|---|---|",
    ]
    for c in v["checks"]:
        lines.append(f"| {c['name']} | {c['display']} | {'통과' if c['ok'] else '미달'} |")

    lines += [
        "",
        "## 아웃오브샘플 지표",
        "",
        f"- 거래 {m['trades']}회 · 승률 {m['win_rate']:.1f}% · PF {m['profit_factor']:.2f}",
        f"- 기대값 {m['expectancy_r']:+.3f}R/거래 ({m['expectancy_krw']:+,.0f}원/거래)",
        f"- 평균 이익 {m['avg_win_r']:+.2f}R · 평균 손실 {m['avg_loss_r']:+.2f}R · "
        f"최대 연속손실 {m['max_consecutive_losses']}회",
        f"- 최대 낙폭 {m['max_drawdown_pct']:.2f}% · 총 비용 {m['total_cost']:,.0f}원",
        "",
        "## 폴드별 결과",
        "",
        "| 폴드 | 테스트 구간 | 선택 파라미터 | 거래 | 순손익 | 기대값(R) |",
        "|---|---|---|---|---|---|",
    ]
    for f in result["folds"]:
        rng = f"{f.test_dates[0]}~{f.test_dates[-1]}" if f.test_dates else "-"
        lines.append(
            f"| {f.index + 1} | {rng} | {f.params_name} | {f.metrics['trades']} "
            f"| {f.metrics['net_pnl']:+,.0f}원 | {f.metrics['expectancy_r']:+.3f} |"
        )

    lines += ["", "## 청산 사유별 (OOS)", "", "| 사유 | 횟수 | 순손익 | 평균 R |", "|---|---|---|---|"]
    for key, b in sorted(m["exit_reasons"].items(), key=lambda x: -x[1]["count"]):
        lines.append(f"| {key} | {b['count']} | {b['net']:+,.0f}원 | {b['avg_r']:+.2f} |")

    if m["monthly"]:
        lines += ["", "## 월별 (OOS)", "", "| 월 | 거래 | 순손익 |", "|---|---|---|"]
        for mon in sorted(m["monthly"]):
            b = m["monthly"][mon]
            lines.append(f"| {mon} | {b['count']} | {b['net']:+,.0f}원 |")

    lines += [
        "",
        "## 해석 시 주의",
        "",
        "- 백테스트 유니버스는 수집된 종목 집합이라 생존·선정 편향이 있다.",
        "- 분봉 종가 체결 가정 + 슬리피지 가정에 따라 실전 결과는 더 나쁠 수 있다.",
        "- BTTrade 비용에는 매도 비용만 반영된다(매수 수수료는 자본에서 별도 차감).",
        "- PASS라도 실전 투입은 모의(paper) 검증 후 소액부터 단계적으로 할 것.",
        "",
    ]
    return "\n".join(lines)
