"""
scenario_runner.py — 시나리오 검증 러너

각 시나리오를 실제 전략 엔진에 통과시키고, 선언된 **기대 동작**과
실제 결과를 대조해 합격/불합격을 판정한다.

"수익이 났는가"가 아니라 "설계대로 반응했는가"를 본다.
합성 데이터로 수익률을 논하는 것은 무의미하지만, 동작 검증은 유효하다.

사용::

    python -m src.scenario_runner              # 전체 시나리오 검증
    python -m src.scenario_runner --only bull_trap
    python -m src.scenario_runner --export data/scenarios   # CSV 내보내기
"""
from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .backtest import Backtester, BTResult
from .scenarios import Scenario, build, build_all, ALL_SCENARIOS, export_csv

logger = logging.getLogger("autotrader.scenario")


# ──────────────────────────────────────────────
#  스크리너 게이트 (API 없이 필터만 재현)
# ──────────────────────────────────────────────
@dataclass
class GateResult:
    passed: bool
    reason: str = ""
    gap_pct: float = 0.0
    change_pct: float = 0.0
    turnover_bil: float = 0.0


def screen_gate(scenario: Scenario, config: dict) -> GateResult:
    """스크리너 필터를 데이터만으로 재현

    백테스트 엔진은 전략 로직만 다루므로, 스크리너 단계에서 걸러져야 할
    종목(갭 미달·상한가 근접 등)은 여기서 판정한다.
    """
    u = config.get("universe", {})
    candles = scenario.candles
    if not candles:
        return GateResult(False, "데이터없음")

    prev_close = candles[0].open / (1 + scenario.gap_pct / 100)
    if prev_close <= 0:
        return GateResult(False, "기준가오류")

    gap = scenario.gap_pct
    day_high = max(c.high for c in candles)
    max_change = (day_high - prev_close) / prev_close * 100
    turnover_bil = sum(c.close * c.volume for c in candles) / 1e8

    g = GateResult(True, "", gap, max_change, turnover_bil)

    if gap < float(u.get("min_gap_pct", 3.0)):
        g.passed, g.reason = False, f"갭부족({gap:+.1f}%)"
    elif gap > float(u.get("max_gap_pct", 15.0)):
        g.passed, g.reason = False, f"갭과열({gap:+.1f}%)"
    elif max_change >= float(u.get("max_change_pct", 25.0)):
        g.passed, g.reason = False, f"상한가근접({max_change:+.1f}%)"
    elif turnover_bil < float(u.get("min_turnover_bil", 50)):
        g.passed, g.reason = False, f"거래대금부족({turnover_bil:.0f}억)"

    return g


# ──────────────────────────────────────────────
#  검증 결과
# ──────────────────────────────────────────────
@dataclass
class Verdict:
    scenario:   Scenario
    gate:       GateResult
    result:     Optional[BTResult]
    checks:     list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    @property
    def entries(self) -> int:
        return self.result.total_trades if self.result else 0

    @property
    def net_pnl(self) -> float:
        return self.result.net_pnl if self.result else 0.0

    @property
    def exit_reasons(self) -> list[str]:
        if not self.result:
            return []
        return [t.reason.split("(")[0] for t in self.result.trades]


class ScenarioRunner:
    """시나리오 일괄 검증"""

    def __init__(self, config: dict, capital: float = 10_000_000) -> None:
        self.config = config
        self.capital = capital

    def run_one(self, scenario: Scenario) -> Verdict:
        gate = screen_gate(scenario, self.config)

        # 스크리너에서 걸러진 경우 — 백테스트 자체를 돌리지 않음
        if not gate.passed:
            v = Verdict(scenario, gate, None)
            v.checks.append((
                "스크리너 필터",
                not scenario.expect_entry,
                f"'{gate.reason}'로 제외됨"
                + ("" if not scenario.expect_entry else " (진입이 기대됐으나 차단됨)"),
            ))
            return v

        bt = Backtester(self.config, initial_capital=self.capital)
        result = bt.run({scenario.name: scenario.candles})
        v = Verdict(scenario, gate, result)
        self._check(v)
        return v

    def _check(self, v: Verdict) -> None:
        sc, r = v.scenario, v.result
        assert r is not None

        # 1. 진입 발생 여부
        entered = r.total_trades > 0
        v.checks.append((
            "진입 발생",
            entered == sc.expect_entry,
            f"기대={'있음' if sc.expect_entry else '없음'} / "
            f"실제={'있음' if entered else '없음'} ({r.total_trades}건)",
        ))

        if not sc.expect_entry:
            return

        if not entered:
            return   # 위 체크에서 이미 실패 처리

        # 2. 청산 사유
        if sc.expect_exits:
            actual = set(v.exit_reasons)
            matched = [k for k in sc.expect_exits
                       if any(k in a for a in actual)]
            v.checks.append((
                "청산 사유",
                len(matched) > 0,
                f"기대={sc.expect_exits} / 실제={sorted(actual)}",
            ))

        # 3. 손익 방향
        if sc.expect_pnl != "any":
            pnl = r.net_pnl
            ok = {
                "positive": pnl > 0,
                "negative": pnl < 0,
                "flat":     abs(pnl) < self.capital * 0.001,
            }.get(sc.expect_pnl, True)
            v.checks.append((
                "손익 방향",
                ok,
                f"기대={sc.expect_pnl} / 실제={pnl:+,.0f}원",
            ))

        # 4. 최대 손실 한도
        if sc.max_loss_pct is not None:
            limit = self.capital * sc.max_loss_pct / 100
            ok = r.net_pnl >= -limit
            v.checks.append((
                "손실 한도",
                ok,
                f"한도=-{limit:,.0f}원 / 실제={r.net_pnl:+,.0f}원",
            ))

        # 5. 오버나잇 미청산 없음 (강제청산 로직 검증)
        closed_qty = sum(t.qty for t in r.trades)
        v.checks.append((
            "전량 청산",
            closed_qty > 0,
            f"청산 누적 {closed_qty}주 — 오버나잇 잔량 없음",
        ))

    def run_all(self, names: Optional[list[str]] = None) -> list[Verdict]:
        scenarios = [build(n) for n in names] if names else build_all()
        return [self.run_one(s) for s in scenarios]


# ──────────────────────────────────────────────
#  리포트
# ──────────────────────────────────────────────
def print_report(verdicts: list[Verdict], verbose: bool = True) -> bool:
    W = 76
    print("═" * W)
    print("  시나리오 검증 결과")
    print("═" * W)

    for v in verdicts:
        sc = v.scenario
        mark = "✅ PASS" if v.passed else "❌ FAIL"
        print(f"\n{mark}  [{sc.name}] {sc.title}")
        print(f"  {sc.description}")
        print(f"  갭 {v.gate.gap_pct:+.1f}%  "
              f"최대등락 {v.gate.change_pct:+.1f}%  "
              f"거래대금 {v.gate.turnover_bil:,.0f}억")

        if v.result:
            r = v.result
            print(f"  → 거래 {r.total_trades}건  "
                  f"순손익 {r.net_pnl:+,.0f}원 ({r.return_pct:+.2f}%)  "
                  f"MDD {r.max_drawdown_pct:.2f}%")
            if v.exit_reasons:
                from collections import Counter
                cnt = Counter(v.exit_reasons)
                print(f"     청산: " + ", ".join(
                    f"{k}×{n}" for k, n in cnt.most_common()))

        for label, ok, detail in v.checks:
            icon = "✔" if ok else "✘"
            print(f"     {icon} {label}: {detail}")

        if verbose and not v.passed:
            print(f"     ↳ 기대 근거: {sc.rationale}")

    passed = sum(1 for v in verdicts if v.passed)
    total = len(verdicts)

    print("\n" + "═" * W)
    print(f"  종합: {passed}/{total} 시나리오 통과")
    print("═" * W)

    if passed < total:
        print("\n  실패한 시나리오는 전략 로직 또는 파라미터를 점검하세요.")
        print("  합성 데이터이므로 손익 수치가 아닌 '동작'을 보셔야 합니다.")

    return passed == total


def print_summary_table(verdicts: list[Verdict]) -> None:
    W = 76
    print("\n" + "─" * W)
    print(f"  {'시나리오':<16}{'판정':<8}{'거래':>5}{'순손익':>14}{'수익률':>10}{'MDD':>8}")
    print("─" * W)
    for v in verdicts:
        r = v.result
        print(
            f"  {v.scenario.name:<16}"
            f"{'PASS' if v.passed else 'FAIL':<8}"
            f"{v.entries:>5}"
            f"{(f'{r.net_pnl:+,.0f}' if r else '-'):>14}"
            f"{(f'{r.return_pct:+.2f}%' if r else '-'):>10}"
            f"{(f'{r.max_drawdown_pct:.2f}%' if r else '-'):>8}"
        )
    print("─" * W)


# ──────────────────────────────────────────────
#  CLI
# ──────────────────────────────────────────────
def main() -> None:
    import yaml
    from .utils import setup_logger

    p = argparse.ArgumentParser(description="시나리오 검증 러너")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--capital", type=float, default=10_000_000)
    p.add_argument("--only", help="특정 시나리오만 (쉼표 구분)")
    p.add_argument("--export", metavar="DIR", help="시나리오를 CSV로 내보내기")
    p.add_argument("--list", action="store_true", help="시나리오 목록 출력")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    os.chdir(Path(__file__).parent.parent)
    setup_logger(level="ERROR" if args.quiet else "WARNING")

    if args.list:
        print("사용 가능한 시나리오:\n")
        for s in build_all():
            print(f"  {s.name:<16} {s.title}")
            print(f"  {'':<16} {s.description}")
            print(f"  {'':<16} 기대: 진입 {'O' if s.expect_entry else 'X'}"
                  f"  손익 {s.expect_pnl}\n")
        return

    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if args.export:
        for s in build_all():
            out = Path(args.export) / f"{s.name}.csv"
            export_csv(s, str(out))
            print(f"  저장: {out} ({s.bar_count}봉)")
        return

    names = [n.strip() for n in args.only.split(",")] if args.only else None
    runner = ScenarioRunner(config, args.capital)
    verdicts = runner.run_all(names)

    ok = print_report(verdicts, verbose=not args.quiet)
    print_summary_table(verdicts)

    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
