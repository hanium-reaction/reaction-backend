"""M33 3-arm 하네스 — **④층이 순이득인가** (실 LLM 호출).

설계는 [`docs/experiments/m33-3arm-design.md`](../docs/experiments/m33-3arm-design.md) 가
**실행 전에** 고정했다. 이 파일은 그것을 그대로 집행한다.

## ⚠️ 프로덕션 노드를 **그대로 부른다** — 옮겨 적지 않는다

`first_plan.validate_inputs` · `decompose_goal` · `review_plan` · `should_replan` ·
`_replan_feedback` 을 **직접 호출**한다. `src/` 는 한 줄도 안 바꾼다.

DB·tone 은 `config = {}` 로 비운다 — `_session`/`_tone_mode` 가 `config.get("configurable", {})`
로 읽어서 `None` 이 되고, LLM 클라이언트는 세션이 없으면 `llm_runs` 기록만 건너뛴다.

**프롬프트를 복사하지 않는 것이 이 설계의 핵심이다.** `_review_variables` 를 옮겨 적었다가
34호출을 통째로 버린 전례가 있다(`l1-7-results.md` §5).

## 세 arm — 차이는 `review` 하나뿐

```
공통   validate_inputs → decompose_goal → review_plan     (한 번만)
       ↓ 검토가 승인이면 → A·B·C 모두 초기 계획 그대로 (셋이 동일)
       ↓ 검토가 반려이면
A      재분해 없음 — 초기 계획 유지
B      review 그대로 재분해        → _replan_feedback 이 feedback[] 전문을 싣는다
C      review.feedback 을 비우고 재분해 → _replan_feedback 이 "(첫 분해 …)" 를 낸다
```

**C 도 프로덕션 함수가 만든다.** `_replan_feedback` 은 `review.feedback` 이 비면 빈 신호를
돌려주므로, **같은 review 에서 리스트만 비우면** 빈 피드백 arm 이 된다 — 프롬프트를 따로
쓰지 않는다.

## 실행

    uv run python scripts/l1_7b_m33_run.py --dry-run              # LLM 없이 구성 확인
    uv run python scripts/l1_7b_m33_run.py --limit 2              # 스모크 (본 실행 전 필수)
    uv run python scripts/l1_7b_m33_run.py --stratum general      # 일반 34건
    uv run python scripts/l1_7b_m33_run.py --stratum challenge    # 도전 16건
    uv run python scripts/l1_7b_m33_run.py --summarize-only

⚠️ **일반과 도전을 섞어 한 수치로 내지 않는다.** `--stratum` 이 필수인 이유다.

원자료 `eval/l1_7b_m33_{stratum}.jsonl` (비결정적이라 `.gitignore`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Final

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from reaction_backend.orchestrator import first_plan as FP  # noqa: E402
from reaction_backend.schemas.planning import MilestoneDraft, PlanReview  # noqa: E402
from scripts.l1_7_run import build_outcome  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

STRATUM_PATHS: Final = {
    "general": _ROOT / "eval" / "golden_first_plan_cases.jsonl",
    "challenge": _ROOT / "eval" / "golden_challenge_stratum.jsonl",
}
ARMS: Final = ("A_none", "B_feedback", "C_retry")
PRIMARY_REPEAT: Final = 0
"""주지표의 1차 추정 회차. **사전 지정**이다 — 결과를 보고 고르면 안 된다."""

BOOTSTRAP_N: Final = 10_000
BOOTSTRAP_SEED: Final = 42
"""설계 §4.2 가 **실행 전에** 고정한 값. 여기서 바꾸면 사전등록을 어긴다."""


def load_stratum(stratum: str, limit: int | None = None) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in STRATUM_PATHS[stratum].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cases = [c for c in rows if c["kind"] == "decompose"]
    return cases[:limit] if limit else cases


def _milestones(case: dict[str, Any]) -> list[MilestoneDraft] | None:
    raw = case["interview"].get("milestones") or []
    return [MilestoneDraft(title=m["title"], summary=m["summary"]) for m in raw] or None


async def run_case(case: dict[str, Any], repeat: int, *, today: date) -> dict[str, Any]:
    """한 케이스의 세 arm. **초기 계획과 검토 판정을 공유한다.**"""
    outcome = build_outcome(case, today=today)
    cfg: Any = {}  # DB·tone 없음 — `_session`/`_tone_mode` 가 None 을 돌려준다
    state = FP.initial_state(
        user_id=uuid.uuid4(),
        outcome=outcome,
        target_date=today.isoformat(),
        milestones=_milestones(case),
        milestone_cursor=case["interview"].get("milestone_cursor", 0),
    )
    row: dict[str, Any] = {
        "case_id": case["case_id"],
        "block": case["block"],
        "repeat": repeat,
        "axes": case.get("axes"),
    }

    # ── 공통: 초기 분해 + 검토 (한 번만) ──────────────────────────────────
    state = await FP.validate_inputs(state, cfg)
    state = await FP.decompose_goal(state, cfg)
    if state["goal_plan"] is None or state["used_fallback"]:
        row["fell_back"] = True
        row["stage"] = "decompose"
        return row
    initial = state["goal_plan"]
    state = await FP.review_plan(state, cfg)
    review = state["review"]
    if review is None:
        row["fell_back"] = True
        row["stage"] = "review"
        return row

    rejected = FP.should_replan(state) == "replan"
    row.update(
        fell_back=False,
        approved=review.approved,
        rejected=rejected,
        feedback=list(review.feedback),
        plans={"A_none": _dump(initial)},
    )

    # ── 승인이면 세 arm 이 같다 (설계 §1.1) ───────────────────────────────
    if not rejected:
        row["plans"]["B_feedback"] = row["plans"]["C_retry"] = _dump(initial)
        return row

    # ── B: review 그대로 재분해 ──────────────────────────────────────────
    b = await FP.decompose_goal(dict(state), cfg)  # type: ignore[arg-type]
    row["plans"]["B_feedback"] = _dump(b["goal_plan"]) if not b["used_fallback"] else None
    row["b_feedback_sent"] = FP._replan_feedback(state)

    # ── C: 같은 review 에서 feedback 만 비운다 ────────────────────────────
    # `_replan_feedback` 이 빈 리스트를 보면 "(첫 분해 …)" 를 낸다 — **프로덕션 함수가
    # 빈 피드백 arm 을 만든다.** 프롬프트를 따로 쓰지 않는다.
    c_state = dict(state)
    c_state["review"] = PlanReview(approved=review.approved, feedback=[])
    row["c_feedback_sent"] = FP._replan_feedback(c_state)  # type: ignore[arg-type]
    c = await FP.decompose_goal(c_state, cfg)  # type: ignore[arg-type]
    row["plans"]["C_retry"] = _dump(c["goal_plan"]) if not c["used_fallback"] else None
    return row


def _dump(plan: Any) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "goal_nodes": [n.model_dump() for n in plan.goal_nodes],
        "action_items": [a.model_dump() for a in plan.action_items],
    }


async def main_async(args: argparse.Namespace) -> None:
    out_path = _ROOT / "eval" / f"l1_7b_m33_{args.stratum}.jsonl"
    if args.summarize_only:
        rows = [
            json.loads(x) for x in out_path.read_text(encoding="utf-8").splitlines() if x.strip()
        ]
        print(f"저장된 원자료 재집계: {out_path.relative_to(_ROOT)} ({len(rows)}행)")
        summarize(rows, stratum=args.stratum)
        return

    today = date.today()
    cases = load_stratum(args.stratum, args.limit)
    calls = len(cases) * args.repeats * 3  # 최악: 분해1 + 검토1 + 재분해2
    print(
        f"[{args.stratum}] 케이스 {len(cases)}건 × 반복 {args.repeats}회 "
        f"→ 최대 {calls}~{calls + len(cases) * args.repeats} 호출"
        f"{' (dry-run)' if args.dry_run else ''}"
    )
    if args.dry_run:
        for c in cases:
            print(f"  {c['case_id']:<16} 마일스톤 {len(c['interview'].get('milestones') or [])}개")
        return

    rows: list[dict[str, Any]] = []
    for repeat in range(args.repeats):
        for case in cases:
            row = await run_case(case, repeat, today=today)
            rows.append(row)
            print(
                "!" if row.get("fell_back") else ("R" if row.get("rejected") else "."),
                end="",
                flush=True,
            )
    print()
    out_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    print(f"원자료: {out_path.relative_to(_ROOT)} ({len(rows)}행)")
    summarize(rows, stratum=args.stratum)


# ─────────────────────────────────────────────────────────────────────────────
# M26-core 집계 — 설계 §4
#
# ⚠️ **기준이 L1-7A 와 다르다.** 여기서 채점하는 것은 `decompose_goal` 이 ③층까지 돌린
# **최종 계획**이다(노드가 원안을 state 에 남기지 않는다). L1-7A 의 M26-core 0.794 는
# **③층 보정 전 원안** 기준이므로 **절대값을 비교하면 안 된다** — 이 실험 안의 ΔM26-core
# 만 의미가 있다.
#
# ⚠️ 지표 계산은 `l1_7_run.score_raw` 와 `l1_7_run.core_verdicts` 를 **그대로 쓴다.**
# 옮겨 적으면 두 실험이 다른 것을 재게 된다.
# ─────────────────────────────────────────────────────────────────────────────


def _arm_verdicts(
    case: dict[str, Any] | None, plan_dump: dict[str, Any] | None, *, today: date
) -> Any:
    """한 arm 의 계획 → `core_verdicts` 판정. 계획이 없으면 `None`."""
    if plan_dump is None or case is None:
        return None
    from reaction_backend.schemas.planning import GoalDecomposition
    from scripts import l1_7_run as R
    from scripts import l1_7_schedule_eval as SE

    outcome = build_outcome(case, today=today)
    plan = GoalDecomposition.model_validate(plan_dump)
    window = R.cycle_window(case, outcome, today)
    row = R.score_raw(
        outcome,
        plan,
        window,
        today,
        case_milestones=len(case["interview"].get("milestones") or []),
    )
    sched = SE.evaluate_case(case, today=today, action_items=plan_dump["action_items"])
    return R.core_verdicts(row, sched)


def paired_deltas(
    rows: list[dict[str, Any]],
    cases: dict[str, dict[str, Any]],
    a: str,
    b: str,
    *,
    today: date,
) -> tuple[list[int], list[str]]:
    """케이스별 `(b 통과 − a 통과)`. **세 arm 모두 정의된 케이스만** 남긴다 (설계 §4.1).

    한 arm 에서만 N/A 가 되면 ΔM26-core 가 **서로 다른 케이스 집합의 차**가 된다.
    """
    from scripts import l1_7_run as R
    from scripts.l1_7_schedule_eval import _NotApplicable

    deltas: list[int] = []
    dropped: list[str] = []
    for r in rows:
        vals: dict[str, bool] = {}
        for arm in ARMS:
            v = _arm_verdicts(cases.get(r["case_id"]), r["plans"].get(arm), today=today)
            if v is None:
                break
            verdict, _ = R.m26_core(v)
            if isinstance(verdict, _NotApplicable):
                break
            vals[arm] = bool(verdict)
        if len(vals) != len(ARMS):
            dropped.append(r["case_id"])
            continue
        deltas.append(int(vals[b]) - int(vals[a]))
    return deltas, dropped


def _arm_m18b(
    case: dict[str, Any] | None, plan_dump: dict[str, Any] | None, *, today: date
) -> float | None:
    """arm 별 M18b — **M26-core 의 AND 에는 없고 나란히 보고한다**(설계 §4)."""
    if case is None or plan_dump is None:
        return None
    from reaction_backend.schemas.planning import GoalDecomposition
    from scripts import l1_7_run as R

    outcome = build_outcome(case, today=today)
    plan = GoalDecomposition.model_validate(plan_dump)
    return R.score_raw(outcome, plan, [], today).get("m18b_ratio")


def paired_bootstrap_ci(deltas: list[int]) -> tuple[float, float, float]:
    """케이스 단위 **페어드** 부트스트랩 — 설계 §4.2 가 실행 전에 고정한 방법.

    `(점추정, 하한, 상한)`. 케이스를 복원 추출한다 — **행이 아니라 케이스**가 표집 단위다.
    행 단위로 재면 같은 케이스의 arm·반복이 상관돼 구간이 **거짓으로 좁아진다.**
    """
    import random

    if not deltas:
        return 0.0, 0.0, 0.0
    point = sum(deltas) / len(deltas)
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(deltas)
    samples = sorted(sum(rng.choices(deltas, k=n)) / n for _ in range(BOOTSTRAP_N))
    lo = samples[int(0.025 * BOOTSTRAP_N)]
    hi = samples[min(BOOTSTRAP_N - 1, int(0.975 * BOOTSTRAP_N))]
    return point, lo, hi


def summarize(rows: list[dict[str, Any]], *, stratum: str) -> None:
    """arm 별 M26-core · M18 과 M33(ΔM26-core) 을 낸다.

    ⚠️ **채점 기준이 L1-7A 와 다르다.** 여기서 채점하는 것은 `decompose_goal` 이 ③층까지
    돌린 **최종 계획**이다(노드가 원안을 state 에 남기지 않는다). L1-7A 의 M26-core 0.794 는
    **원안 기준**이므로 **절대값을 비교하면 안 된다** — 이 실험 안의 Δ 만 의미가 있다.
    """
    import statistics as _st

    from scripts import l1_7_run as R
    from scripts.l1_7_schedule_eval import _NotApplicable

    cases = {c["case_id"]: c for c in load_stratum(stratum)}
    today = date.today()
    ok = [r for r in rows if not r.get("fell_back")]
    fb = [r for r in rows if r.get("fell_back")]
    primary = [r for r in ok if r["repeat"] == PRIMARY_REPEAT]
    rej = [r for r in primary if r.get("rejected")]

    print(f"\n{'=' * 74}\nM33 3-arm [{stratum}]")
    print(f"실행 {len(rows)}행 / 집계 {len(ok)} / 폴백 {len(fb)}")
    if fb:
        print(f"  폴백 단계: {[r.get('stage') for r in fb][:6]}")
    print(f"\nrepeat {PRIMARY_REPEAT} 고유 {len(primary)}건 중 **반려 {len(rej)}건**")
    print("   ⚠️ 승인 케이스는 세 arm 이 같으므로 **ΔM26-core 에 0 을 기여**한다(설계 §1.1).")
    if rej:
        print(f"   반려: {', '.join(r['case_id'] for r in rej[:8])}")
        bad = [r for r in rej if not r["plans"].get("B_feedback") or not r["plans"].get("C_retry")]
        if bad:
            print(f"   ⚠️ 재분해가 폴백한 건 {len(bad)}건 — 페어링에서 빠진다")
    if not primary:
        print("=" * 74)
        return

    # ── arm 별 M26-core ──────────────────────────────────────────────────
    print("\n── M26-core (arm 별) · **최종 계획 기준**")
    for arm in ARMS:
        p = f = na = 0
        for r in primary:
            v = _arm_verdicts(cases.get(r["case_id"]), r["plans"].get(arm), today=today)
            if v is None:
                na += 1
                continue
            verdict, _ = R.m26_core(v)
            if isinstance(verdict, _NotApplicable):
                na += 1
            elif verdict:
                p += 1
            else:
                f += 1
        den = p + f
        print(f"   {arm:<12} {(f'{p / den:.3f}' if den else '—'):>6} ({p}/{den})   N/A {na}")

    # ── M33 = ΔM26-core (B − A) ──────────────────────────────────────────
    print("\n── M33 = ΔM26-core (B − A) · 케이스 단위 페어드 부트스트랩")
    deltas, dropped = paired_deltas(primary, cases, "A_none", "B_feedback", today=today)
    if deltas:
        pt, lo, hi = paired_bootstrap_ci(deltas)
        if lo > 0:
            verdict = "④층 유지"
        elif hi < 0:
            verdict = "**이 층을 룰로 대체하거나 걷어내자**"
        else:
            verdict = "부호 미결정 — **억지로 만들지 않는다**"
        print(f"   M33 = {pt:+.4f}   95% CI [{lo:+.4f}, {hi:+.4f}]   n={len(deltas)}")
        print(f"   → {verdict}   ({BOOTSTRAP_N}회 · 시드 {BOOTSTRAP_SEED})")
    else:
        print("   페어링 가능한 케이스가 없다")
    if dropped:
        print(f"   ⚠️ 페어링에서 빠진 {len(dropped)}건: {', '.join(dropped[:6])}")
        print("      한 arm 에서만 N/A 면 **서로 다른 케이스 집합의 차**가 된다(설계 §4.1)")

    # ── M18 은 AND 에 없고 **나란히** 본다 ───────────────────────────────
    print("\n── M18b (arm 별 분포) — **M26-core 와 나란히 본다**")
    for arm in ARMS:
        vals = [
            x
            for r in primary
            for x in [_arm_m18b(cases.get(r["case_id"]), r["plans"].get(arm), today=today)]
            if x is not None
        ]
        if vals:
            print(
                f"   {arm:<12} 중앙 {_st.median(vals):.3f} · "
                f"미달 {sum(1 for x in vals if x < 1)}/{len(vals)}"
            )

    print(
        f"\n⚠️ 일반/도전을 **섞지 않는다** — 이 파일은 [{stratum}] 전용이다.\n"
        "⚠️ 채점은 **최종 계획** 기준이라 L1-7A 의 원안 기준 M26-core 와 **절대값을 비교하지\n"
        f"   않는다** — 이 실험 안의 Δ 만 의미가 있다.\n{'=' * 74}"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="M33 3-arm (실 LLM 호출)")
    p.add_argument(
        "--stratum",
        choices=("general", "challenge"),
        required=True,
        help="**필수** — 일반과 도전을 섞어 한 수치로 내지 않는다(설계 §3.4)",
    )
    p.add_argument("--limit", type=int, default=None, help="앞 N건만 (스모크)")
    p.add_argument("--repeats", type=int, default=1, help="케이스당 반복")
    p.add_argument("--dry-run", action="store_true", help="LLM 없이 구성만")
    p.add_argument("--summarize-only", action="store_true", help="저장된 원자료만 재집계")
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
