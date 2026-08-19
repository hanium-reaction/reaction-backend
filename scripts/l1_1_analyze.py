"""L1-1 분석 — 판정 결과를 사전등록 §3~§5 규칙으로 집계한다. LLM 호출 없음, 순수 계산.

`l1_1_judge.py` 가 만든 `eval/l1_1_judgments.jsonl` 을 읽어:

1. **swap consistency 로 판정을 케이스 수준 결과로 정리**한다(정방향/역방향 승자가 같으면
   채택, 다르면 "판정 불일치"로 제외) — `docs/experiments/preregistration-v1.md` §4 step 2.
2. **1차 지표**: v3 vs v1 pairwise 승률 + 케이스 단위 클러스터 부트스트랩 95% CI(§3).
3. **성공 기준 3개 AND**(§5): ① 승률≥0.65 AND CI 하한>0.50 ② swap consistency≥0.80
   ③ 어떤 태그 층(골든셋 `block`)에서도 v3 승률 0.35 미만 없음.
4. 2차 지표(참고용, 성공 기준에 안 씀): v1-v2/v2-v3 승률, 버전별 축④ 실격률, 무승부 비율.

아무것도 쓰지 않는다 — 표준출력에 보고서만 낸다(선례: `report_llm_run_metrics.py`,
`report_recovery_followthrough.py`).

실행:
  uv run python -m scripts.l1_1_analyze
  uv run python -m scripts.l1_1_analyze --bootstrap-iterations 2000  # 빠른 확인용
"""

from __future__ import annotations

import argparse
import logging
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from scripts.l1_1_common import (
    JUDGMENTS_PATH,
    JudgmentRow,
    load_golden_cases,
    read_judgments,
)

_log = logging.getLogger(__name__)

DEFAULT_BOOTSTRAP_ITERATIONS = 10_000
BOOTSTRAP_SEED = 20260819  # l1_1_judge.RANDOM_SEED 와 별개 — 분석은 분석대로 재현 고정.

# 사전등록 §5 성공 기준.
SUCCESS_WIN_RATE_THRESHOLD = 0.65
SUCCESS_CI_LOWER_BOUND = 0.50
SUCCESS_SWAP_CONSISTENCY_THRESHOLD = 0.80
SUCCESS_MIN_TAG_STRATUM_WIN_RATE = 0.35

PRIMARY_PAIR = "1-3"


class ResolvedOutcome(NamedTuple):
    """swap consistency 를 통과한 판정 1건 — 케이스 단위 클러스터 부트스트랩의 원소."""

    case_id: str
    pair: str
    winner_version: str | None  # None = 무승부


@dataclass(slots=True)
class SwapConsistencyResult:
    resolved: list[ResolvedOutcome]
    inconsistent_n: int
    incomplete_n: int  # 정방향/역방향 중 한쪽만 있어 판단 불가(심판 호출 실패로 버려진 쪽)
    total_pairs_with_both_directions: int

    @property
    def consistency_rate(self) -> float | None:
        if self.total_pairs_with_both_directions == 0:
            return None
        consistent = self.total_pairs_with_both_directions - self.inconsistent_n
        return consistent / self.total_pairs_with_both_directions


def resolve_swap_consistency(rows: list[JudgmentRow]) -> SwapConsistencyResult:
    """정방향/역방향 쌍을 하나의 케이스 수준 결과로 정리한다 (사전등록 §4 step 2)."""
    by_unit: dict[tuple[str, str, int], list[JudgmentRow]] = defaultdict(list)
    for row in rows:
        by_unit[(row.case_id, row.pair, row.rep_index)].append(row)

    resolved: list[ResolvedOutcome] = []
    inconsistent_n = 0
    incomplete_n = 0
    total_both = 0

    for (case_id, pair, _rep), unit_rows in by_unit.items():
        forward = [r for r in unit_rows if not r.swap]
        reversed_ = [r for r in unit_rows if r.swap]
        if not forward or not reversed_:
            incomplete_n += len(unit_rows)
            continue
        total_both += 1
        winner_fwd = forward[0].winner_version()
        winner_rev = reversed_[0].winner_version()
        if winner_fwd == winner_rev:
            resolved.append(ResolvedOutcome(case_id=case_id, pair=pair, winner_version=winner_fwd))
        else:
            inconsistent_n += 1

    return SwapConsistencyResult(
        resolved=resolved,
        inconsistent_n=inconsistent_n,
        incomplete_n=incomplete_n,
        total_pairs_with_both_directions=total_both,
    )


def win_rate(
    outcomes: list[ResolvedOutcome], *, winner: str, loser: str
) -> tuple[float | None, int, int, int]:
    """`winner`/`loser` 승/패/무승부 개수 → (승률, 승, 패, 무). 승+패=0 이면 승률 None."""
    wins = sum(1 for o in outcomes if o.winner_version == winner)
    losses = sum(1 for o in outcomes if o.winner_version == loser)
    draws = sum(1 for o in outcomes if o.winner_version is None)
    denom = wins + losses
    return (wins / denom if denom else None), wins, losses, draws


def _percentile(values: list[float], pct: float) -> float:
    """최근접 순위(nearest-rank) 백분위수 — `report_llm_run_metrics.py` 와 같은 방식."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return ordered[rank]


def cluster_bootstrap_ci(
    outcomes: list[ResolvedOutcome],
    *,
    winner: str,
    loser: str,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float] | None:
    """케이스 단위 클러스터 부트스트랩 95% CI (사전등록 §3 — 판정이 아니라 케이스를 리샘플).

    같은 케이스의 여러 판정(최대 2쌍 × 2 pair 조합에서 온 outcome)이 상관돼 있으므로,
    판정을 독립 베르누이처럼 리샘플하지 않고 **케이스 전체를 단위로** 리샘플한다.
    """
    by_case: dict[str, list[ResolvedOutcome]] = defaultdict(list)
    for o in outcomes:
        by_case[o.case_id].append(o)
    case_ids = sorted(by_case)
    if not case_ids:
        return None

    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(iterations):
        sampled_cases = [case_ids[rng.randrange(len(case_ids))] for _ in range(len(case_ids))]
        pooled: list[ResolvedOutcome] = []
        for cid in sampled_cases:
            pooled.extend(by_case[cid])
        rate, wins, losses, _draws = win_rate(pooled, winner=winner, loser=loser)
        if rate is not None:
            estimates.append(rate)

    if not estimates:
        return None
    return _percentile(estimates, 2.5), _percentile(estimates, 97.5)


def _load_case_blocks() -> dict[str, str]:
    """골든셋 case_id → block(단일 태그/복합 태그/미매칭 3태그/경계·분기/적대적)."""
    return {c["case_id"]: c["block"] for c in load_golden_cases()}


def tag_stratum_win_rates(
    outcomes: list[ResolvedOutcome], case_blocks: dict[str, str], *, winner: str, loser: str
) -> dict[str, tuple[float | None, int]]:
    """블록별 v3 승률 — (승률, 표본 N=승+패). 사전등록 §5 성공 기준 ③."""
    by_block: dict[str, list[ResolvedOutcome]] = defaultdict(list)
    for o in outcomes:
        block = case_blocks.get(o.case_id, "UNKNOWN")
        by_block[block].append(o)

    result: dict[str, tuple[float | None, int]] = {}
    for block, block_outcomes in by_block.items():
        rate, wins, losses, _draws = win_rate(block_outcomes, winner=winner, loser=loser)
        result[block] = (rate, wins + losses)
    return result


def disqualification_rates(rows: list[JudgmentRow]) -> dict[str, tuple[int, int]]:
    """버전별 축④ 실격 횟수/등장 횟수 (2차 지표, 루브릭 §5 한계 표가 요구하는 별도 병기)."""
    appearances: Counter[str] = Counter()
    disqualified: Counter[str] = Counter()
    for row in rows:
        appearances[row.version_a] += 1
        appearances[row.version_b] += 1
        if row.axis_a[3] == 1:
            disqualified[row.version_a] += 1
        if row.axis_b[3] == 1:
            disqualified[row.version_b] += 1
    return {v: (disqualified[v], appearances[v]) for v in appearances}


def _print_report(
    *,
    swap_all: SwapConsistencyResult,
    swap_primary: SwapConsistencyResult,
    primary_rate: float | None,
    primary_wins: int,
    primary_losses: int,
    primary_draws: int,
    ci: tuple[float, float] | None,
    tag_rates: dict[str, tuple[float | None, int]],
    secondary_v1v2: tuple[float | None, int, int, int],
    secondary_v2v3: tuple[float | None, int, int, int],
    dq_rates: dict[str, tuple[int, int]],
) -> bool:
    print("=" * 60)
    print("L1-1 분석 결과 (docs/experiments/preregistration-v1.md §3~§5)")
    print("=" * 60)

    print(
        f"\n[전체 swap consistency] {_fmt_rate(swap_all.consistency_rate)}"
        f" ({swap_all.total_pairs_with_both_directions - swap_all.inconsistent_n}"
        f"/{swap_all.total_pairs_with_both_directions}, 불완전 {swap_all.incomplete_n}건 제외)"
    )
    print(
        f"[v3 vs v1(pair={PRIMARY_PAIR}) swap consistency] {_fmt_rate(swap_primary.consistency_rate)}"
        f" ({swap_primary.total_pairs_with_both_directions - swap_primary.inconsistent_n}"
        f"/{swap_primary.total_pairs_with_both_directions})"
    )

    print(
        f"\n[1차 지표] v3 vs v1 승률 = {_fmt_rate(primary_rate)}"
        f" (승 {primary_wins} / 패 {primary_losses} / 무 {primary_draws})"
    )
    if ci is not None:
        print(f"  95% cluster bootstrap CI = [{ci[0]:.3f}, {ci[1]:.3f}]")
    else:
        print("  CI 계산 불가 (표본 부족)")

    print("\n[태그 층별 v3 승률] (사전등록 §5 성공 기준 ③)")
    for block, (rate, n) in sorted(tag_rates.items()):
        print(f"  {block:<15} {_fmt_rate(rate)}  (N={n})")

    print("\n[2차 지표 — 참고용, 성공 기준에 안 씀]")
    print(
        f"  v1 vs v2 승률 = {_fmt_rate(secondary_v1v2[0])} (승 {secondary_v1v2[1]}/패 {secondary_v1v2[2]}/무 {secondary_v1v2[3]})"
    )
    print(
        f"  v2 vs v3 승률 = {_fmt_rate(secondary_v2v3[0])} (승 {secondary_v2v3[1]}/패 {secondary_v2v3[2]}/무 {secondary_v2v3[3]})"
    )
    print("  버전별 축④(톤) 실격률:")
    for version, (dq, n) in sorted(dq_rates.items()):
        rate = dq / n if n else None
        print(f"    v{version}: {_fmt_rate(rate)} ({dq}/{n})")

    print("\n" + "=" * 60)
    print("[성공 기준 판정] (사전등록 §5 — 3개 AND, 하나라도 미달이면 실패)")

    c1 = primary_rate is not None and primary_rate >= SUCCESS_WIN_RATE_THRESHOLD
    c1 = c1 and ci is not None and ci[0] > SUCCESS_CI_LOWER_BOUND
    print(
        f"  ① 승률≥{SUCCESS_WIN_RATE_THRESHOLD} AND CI하한>{SUCCESS_CI_LOWER_BOUND}: "
        f"{'PASS' if c1 else 'FAIL'}"
    )

    c2 = (
        swap_primary.consistency_rate is not None
        and swap_primary.consistency_rate >= SUCCESS_SWAP_CONSISTENCY_THRESHOLD
    )
    print(f"  ② swap consistency≥{SUCCESS_SWAP_CONSISTENCY_THRESHOLD}: {'PASS' if c2 else 'FAIL'}")

    below_threshold = [
        (block, rate)
        for block, (rate, n) in tag_rates.items()
        if rate is not None and rate < SUCCESS_MIN_TAG_STRATUM_WIN_RATE
    ]
    c3 = not below_threshold
    print(
        f"  ③ 모든 태그 층 v3 승률≥{SUCCESS_MIN_TAG_STRATUM_WIN_RATE}: {'PASS' if c3 else 'FAIL'}"
        + (f" (미달: {below_threshold})" if below_threshold else "")
    )

    overall = c1 and c2 and c3
    print(f"\n  종합: {'✅ 성공' if overall else '❌ 실패'}")
    print("=" * 60)
    return overall


def _fmt_rate(rate: float | None) -> str:
    return f"{rate:.3f}" if rate is not None else "N/A"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="l1_1_analyze", description="L1-1 판정 결과 분석 (사전등록 §3~§5)."
    )
    parser.add_argument("--bootstrap-iterations", type=int, default=DEFAULT_BOOTSTRAP_ITERATIONS)
    parser.add_argument("--input", default=None, help="입력 경로 (기본: eval/l1_1_judgments.jsonl)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    rows = read_judgments(JUDGMENTS_PATH if args.input is None else Path(args.input))
    _log.info("판정 %d행 로드", len(rows))
    if not rows:
        _log.error("판정 데이터가 없다 — l1_1_judge.py 를 먼저 돌릴 것")
        return 1

    swap_all = resolve_swap_consistency(rows)
    swap_primary = resolve_swap_consistency([r for r in rows if r.pair == PRIMARY_PAIR])

    primary_outcomes = swap_primary.resolved
    primary_rate, primary_wins, primary_losses, primary_draws = win_rate(
        primary_outcomes, winner="3", loser="1"
    )
    ci = cluster_bootstrap_ci(
        primary_outcomes, winner="3", loser="1", iterations=args.bootstrap_iterations
    )

    case_blocks = _load_case_blocks()
    tag_rates = tag_stratum_win_rates(primary_outcomes, case_blocks, winner="3", loser="1")

    swap_v1v2 = resolve_swap_consistency([r for r in rows if r.pair == "1-2"])
    swap_v2v3 = resolve_swap_consistency([r for r in rows if r.pair == "2-3"])
    secondary_v1v2 = win_rate(swap_v1v2.resolved, winner="2", loser="1")
    secondary_v2v3 = win_rate(swap_v2v3.resolved, winner="3", loser="2")

    dq_rates = disqualification_rates(rows)

    # 종료 코드는 "이 스크립트가 정상 실행됐는가"만 나타낸다 — 성공 기준 충족 여부(1차
    # 지표가 미달)는 스크립트 오류가 아니라 유효한 연구 결과이므로 0/1 로 표현하지 않는다.
    # PASS/FAIL 은 아래 보고서 텍스트로만 전달한다.
    _print_report(
        swap_all=swap_all,
        swap_primary=swap_primary,
        primary_rate=primary_rate,
        primary_wins=primary_wins,
        primary_losses=primary_losses,
        primary_draws=primary_draws,
        ci=ci,
        tag_rates=tag_rates,
        secondary_v1v2=secondary_v1v2,
        secondary_v2v3=secondary_v2v3,
        dq_rates=dq_rates,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
