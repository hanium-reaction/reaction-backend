"""L1-7B 하네스 (1단계) — `plan_quality.v3` 검토기 **단독** 평가 (실 LLM 호출).

고정된 `verify` 계획 50건을 ④층 검토기에만 넣는다. 분해(②층)를 돌리지 않는 이유는
계획서 §2 L1-7B 가 못박은 그대로다 — **검토기를 재려면 입력 계획이 고정돼야 한다.**
매번 새로 분해하면 검토기 성능과 분해기 변동이 한 수치에 섞인다.

## 이 실행이 재는 것 — 정확히 이 문장으로 보고한다

> **현재 프로덕션 `plan_quality.v3` 가, ③층 보정을 거쳐 이미 정상인 계획을 얼마나 잘못
> 반려하는가.**

"검토기 층 일반" 의 오탐률이 아니다. v3 는 `focus_capacity`·`session_length` 변수를
받는데, 루브릭 §1.2 는 그 두 항목을 **④층이 검사하면 안 되는 것**으로 분류한다 — ③층이
이미 불변식으로 보장하므로 상한 초과는 보정 후 **0/1,620**(구조적으로 발화 불가)이다.
따라서 그 항목으로 인한 반려는 **정의상 전부 오탐**이고, M29 가 높게 나오는 것은
"검토기가 나쁘다" 가 아니라 **현재 v3 설계가 실제로 만드는 오탐 비용**이다.
그 비용이 v4 와 비교할 기준선이다.

## 지표와 표본 단위 — **여기가 가장 중요하다**

| 지표 | 분모 | 단위 |
|---|---|---|
| **M29** `false_reject_rate` | 무결함 대조군 **30건** | **사전 지정된 1회 호출(repeat 0)** |
| **M27a** `verifier_rejection_rate` | 심은 결함 20건 — **유형별로 따로** | 〃 |
| **M32** `verifier_self_consistency` | 케이스당 반복 판정 쌍 | 3회 전부 |
| `any_of_3_reject_rate` | 대조군 30건 | 보조 — **M29 가 아니다** |

⚠️ **반복 3회를 90개 독립 표본으로 세지 않는다.** 같은 케이스의 반복은 상관이 있어
신뢰구간이 거짓으로 좁아진다. M29 의 1차 추정은 **repeat 0 의 30건**이고, 2·3회차는
M32 와 변동 보고 몫이다. "3회 중 한 번이라도 반려" 는 유용하지만 **별도 이름**을 붙인다.

⚠️ **0/30 의 단측 95% 이항 상한은 0.095** 다(`1 − 0.05^(1/n)`). rule of three(`3/n = 0.100`)
는 근사이고 하필 임계값과 같은 값이라 "겨우 걸친다" 로 오독된다 — 정확값을 쓴다.

## M27a 를 읽을 때

**"v3 가 이 결함의 의미를 이해해 탐지했다" 로 읽으면 안 된다.** 이 골든셋에는 유형별로
남은 지름길이 있다 — 특히 D2 의 `마저` 는 무결함 대조군에서 발화가 0건인 완전 분리자라,
한 줄짜리 문자열 규칙으로도 2/2 를 맞힌다(`eval/README.md` 「M27·M28 을 읽을 때」).
말할 수 있는 것은 **"이 고정 골든셋에서 반려했다"** 까지다.

M27b(옳은 유형을 짚었는가)·M28a·M28b 는 `plan_quality.v4` 가 있어야 계산된다. v3 는
`{approved, feedback: list[str]}` 뿐이라 자유 문장을 기계가 못 센다.

## 실행

    uv run python scripts/l1_7b_run.py --dry-run          # LLM 없이 구성 확인
    uv run python scripts/l1_7b_run.py --limit 3          # 스모크 (본 실행 전 필수)
    uv run python scripts/l1_7b_run.py --repeats 3        # 본 실행
    uv run python scripts/l1_7b_run.py --summarize-only   # 저장된 원자료 재집계

원자료 `eval/l1_7b_results.jsonl` (비결정적이라 `.gitignore`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from reaction_backend.orchestrator import first_plan_adapter
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)
from reaction_backend.schemas.planning import PlanReview

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = _ROOT / "eval" / "golden_first_plan_cases.jsonl"
RESULTS_PATH = _ROOT / "eval" / "l1_7b_results.jsonl"
KST = timezone(timedelta(hours=9))

# M29 의 1차 추정에 쓰는 반복 회차. **사전 지정**이다 — 결과를 보고 고르면 안 된다.
PRIMARY_REPEAT = 0


def one_sided_upper_95(k: int, n: int) -> float:
    """Clopper-Pearson 단측 95% 상한 — scipy 없이.

    `k=0` 이면 닫힌 형태 `1 − 0.05^(1/n)`. 그 외에는 이분 탐색으로
    `P(X ≤ k | n, p) = 0.05` 인 p 를 찾는다(정규 근사를 쓰지 않는다 — n=30, k=0~2 구간에서
    근사는 신뢰구간을 눈에 띄게 왜곡한다).
    """
    if n <= 0:
        return 1.0
    if k <= 0:
        return 1.0 - 0.05 ** (1 / n)
    if k >= n:
        return 1.0

    def cdf(p: float) -> float:
        # P(X <= k) = sum_{i=0..k} C(n,i) p^i (1-p)^(n-i)
        total, comb = 0.0, 1.0
        for i in range(k + 1):
            if i:
                comb = comb * (n - i + 1) / i
            total += comb * (p**i) * ((1 - p) ** (n - i))
        return total

    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if cdf(mid) > 0.05:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def load_cases(limit: int | None = None, blocks: list[str] | None = None) -> list[dict[str, Any]]:
    """`verify` 케이스만 읽는다 — 계획이 이미 고정돼 있는 것들."""
    rows = [
        json.loads(line)
        for line in CASES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cases = [c for c in rows if c["kind"] == "verify"]
    if blocks:
        cases = [c for c in cases if c["block"] in blocks]
    return cases[:limit] if limit else cases


def build_outcome(case: dict[str, Any], *, today: date) -> InterviewOutcome:
    """`l1_7_run.build_outcome` 과 같은 복원 — 마감은 상대 오프셋이다."""
    interview, goal = case["interview"], case["interview"]["goal"]
    deadline = (today + timedelta(days=goal["deadline_offset_days"])).isoformat()
    return InterviewOutcome(
        session_id=f"l1-7b-{case['case_id']}",
        generated_at=datetime(today.year, today.month, today.day, 9, 0, tzinfo=KST),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role=interview["role"], season=interview["season"]),
        core_goals=[
            GoalCandidate(
                title=goal["title"],
                category=goal["category"],
                is_heaviest=True,
                tentative_tier="focus",
                confidence=0.9,
                deadline=deadline,
                success_image=goal["success_image"],
                current_level=goal["current_level"],
                session_length_min=goal["session_length_min"],
                weekly_hours=goal["weekly_hours"],
                frequency_per_week=goal["frequency_per_week"],
                preferred_time=interview["preferred_time"],
                approach_note=goal.get("approach_note"),
            )
        ],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:00"),
            peak_window=[interview["preferred_time"]],
        ),
        preferences=PreferenceProfile(
            recovery_tone="담백",
            rest_ok=True,
            downscope_unit_min=15,
            focus_duration_min=interview["focus_duration_min"],
        ),
        horizon=deadline,
    )


def review_variables(
    case: dict[str, Any], outcome: InterviewOutcome, today: date
) -> dict[str, str]:
    """`first_plan._review_variables` 와 **같은 6개 변수**를 만든다.

    프로덕션 쪽은 `FirstPlanState` 를 받아 그대로 못 부른다. 계약을 옮겨 적었으므로
    **그쪽을 고치면 여기도 고쳐야 한다** — 갈리면 검토기가 프로덕션과 다른 것을 본다.

    ⚠️ `focus_capacity`·`session_length` 를 **일부러 그대로 넘긴다.** 루브릭 §1.2 는 v4 에서
    이 변수를 빼라고 하지만, 이 실행의 목적이 **현행 v3 의 오탐 비용을 재는 것**이므로
    빼면 재려던 것을 못 잰다.

    `conflict_report` 는 스케줄러 결과가 없어 "(없음)" 이다 — 이 하네스는 배치를 돌리지
    않는다. 프로덕션에서는 충돌이 있으면 문장이 들어가고, 루브릭 §1.2 는 그것을
    **검사 항목이 아니라 전달 항목**으로 규정한다(그걸 근거로 반려하면 안 된다).
    """
    ctx = first_plan_adapter.context_from_outcome(outcome, target_date=today)
    prompt_vars: dict[str, str] = ctx["prompt_vars"]
    session_length = str(prompt_vars.get("session_length", "(미입력)"))
    plan = case["plan"]
    return {
        "goal_nodes_json": json.dumps(plan["goal_nodes"], ensure_ascii=False),
        "action_items_json": json.dumps(plan["action_items"], ensure_ascii=False),
        "session_length": session_length,
        "focus_capacity": str(prompt_vars.get("focus_capacity", session_length)),
        "time_policy_summary": str(prompt_vars.get("time_policy_summary", "")),
        "conflict_report": "(없음)",
    }


async def run_case(
    case: dict[str, Any], repeat: int, *, today: date, dry_run: bool
) -> dict[str, Any]:
    from reaction_backend.config import get_settings
    from reaction_backend.llm import aiClient

    outcome = build_outcome(case, today=today)
    variables = review_variables(case, outcome, today)
    row: dict[str, Any] = {
        "case_id": case["case_id"],
        "block": case["block"],
        "repeat": repeat,
        "expected_approved": case["expected"]["approved"],
    }
    if case["block"] == "seeded_defect":
        row["defect"] = case["seeded"]["defect"]
        row["level"] = case["seeded"]["level"]
    if dry_run:
        row["vars"] = {k: (v[:60] if isinstance(v, str) else v) for k, v in variables.items()}
        return row

    settings = get_settings()
    result = await aiClient.run(
        module="planning",
        schema=PlanReview,
        prompt_id="planning/plan_quality",
        # ⚠️ 룰 폴백은 "검토기가 뭘 했나" 에 대해 아무것도 말하지 않는다 — 집계에서 뺀다.
        # 프로덕션의 `_rule_review` 를 흉내내면 그 승인이 M29 를 낮추는 쪽으로 섞인다.
        fallback=lambda: PlanReview(approved=True, feedback=[]),
        timeout=settings.llm_planning_timeout_seconds,
        thinking_budget=settings.llm_planning_thinking_budget,
        variables=variables,
        session=None,
        user_id=None,
    )
    row.update(
        fell_back=result.fell_back,
        reason=result.reason,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        latency_ms=result.latency_ms,
    )
    if not result.fell_back:
        # 모델 출력을 그대로 남긴다 — 집계 정의가 바뀌어도 재호출 없이 되살릴 수 있게.
        row["approved"] = result.value.approved
        row["feedback"] = list(result.value.feedback)
    return row


def _pct(num: int, den: int) -> str:
    return "—" if den == 0 else f"{num / den:.3f} ({num}/{den})"


def summarize(rows: list[dict[str, Any]]) -> None:
    ok = [r for r in rows if not r.get("fell_back") and "approved" in r]
    fb = [r for r in rows if r.get("fell_back")]
    print(f"\n{'=' * 74}\nL1-7B 1단계 — `plan_quality.v3` 검토기 단독")
    print(f"실행 {len(rows)}건 / 집계 {len(ok)}건 / 룰 폴백 {len(fb)}건(집계 제외)")
    if fb:
        print("  폴백 사유:", dict(Counter(r.get("reason") or "?" for r in fb)))
    if not ok:
        print("  집계할 것이 없다.")
        return

    primary = [r for r in ok if r["repeat"] == PRIMARY_REPEAT]

    # ── M29 — 주 지표 ────────────────────────────────────────────────────
    ctl = [r for r in primary if r["block"] == "defect_free_control"]
    if ctl:
        rejected = [r for r in ctl if not r["approved"]]
        n, k = len(ctl), len(rejected)
        upper = one_sided_upper_95(k, n)
        verdict = "임계값 아래" if upper <= 0.10 else "**임계값을 확인할 수 없다**"
        print(f"\n── M29 `false_reject_rate` (주 지표, repeat {PRIMARY_REPEAT} 의 고유 {n}건)")
        print(
            f"   오반려 {_pct(k, n)}   단측 95% 상한 {upper:.3f}  → 사전등록 ≤0.10 대비 {verdict}"
        )
        if rejected:
            print(f"   반려된 대조군: {', '.join(sorted(r['case_id'] for r in rejected))}")

    # ── M27a — 유형별 ────────────────────────────────────────────────────
    seeded = [r for r in primary if r["block"] == "seeded_defect"]
    if seeded:
        print(f"\n── M27a `verifier_rejection_rate` (repeat {PRIMARY_REPEAT}, **유형별**)")
        print("   ⚠️ '의미를 이해해 탐지했다' 가 아니라 **'이 고정 골든셋에서 반려했다'** 까지만.")
        by: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in seeded:
            by[r["defect"]].append(r)
        for defect in sorted(by):
            sub = by[defect]
            easy = [r for r in sub if r["level"] == "easy"]
            bnd = [r for r in sub if r["level"] == "boundary"]
            e_hit = sum(1 for r in easy if not r["approved"])
            b_hit = sum(1 for r in bnd if not r["approved"])
            print(
                f"   {defect}  easy 반려 {_pct(e_hit, len(easy))} (정답=반려) · "
                f"boundary 반려 {_pct(b_hit, len(bnd))} (정답=통과 — 반려는 오탐)"
            )

    # ── M32 — 반복 일관성 ────────────────────────────────────────────────
    reps = sorted({r["repeat"] for r in ok})
    if len(reps) > 1:
        by_case: dict[str, list[bool]] = defaultdict(list)
        for r in ok:
            by_case[r["case_id"]].append(bool(r["approved"]))
        pairs = agree = 0
        flips = []
        for cid, vs in by_case.items():
            for i in range(len(vs)):
                for j in range(i + 1, len(vs)):
                    pairs += 1
                    agree += vs[i] == vs[j]
            if len(set(vs)) > 1:
                flips.append(cid)
        print(f"\n── M32 `verifier_self_consistency` ({len(reps)}회, 케이스당 판정 쌍)")
        print(
            f"   같은 판정 {_pct(agree, pairs)}   판정이 갈린 케이스 {_pct(len(flips), len(by_case))}"
        )
        if flips:
            print(f"   갈린 케이스: {', '.join(sorted(flips)[:8])}")

        # 보조 — **M29 가 아니다**
        ctl_ids = {r["case_id"] for r in ok if r["block"] == "defect_free_control"}
        any_rej = sum(1 for c in ctl_ids if any(not v for v in by_case[c]))
        print(
            f"\n   [보조] `any_of_{len(reps)}_reject_rate` (대조군): {_pct(any_rej, len(ctl_ids))}"
        )
        print("   ⚠️ **M29 가 아니다.** 반복을 독립 표본으로 세면 신뢰구간이 거짓으로 좁아진다.")

    lat = sorted(r["latency_ms"] for r in ok if r.get("latency_ms"))
    if lat:
        import math

        print(
            f"\n── 시스템 : 지연 중앙 {statistics.median(lat):.0f}ms · "
            f"p95 {lat[min(len(lat) - 1, math.ceil(0.95 * len(lat)) - 1)]:.0f}ms · "
            f"토큰 in {sum(r.get('tokens_in') or 0 for r in ok)} / "
            f"out {sum(r.get('tokens_out') or 0 for r in ok)}"
        )
    print(
        "\n⚠️ 이 수치는 **현행 v3 의** 오탐 비용이다 — v3 는 루브릭 §1.2 가 금지한\n"
        "   `focus_capacity`·`session_length` 를 받는다. '검토기 층 일반' 으로 읽지 말 것.\n"
        f"⚠️ M27b·M28a·M28b 는 `plan_quality.v4` 가 있어야 계산된다.\n{'=' * 74}"
    )


async def main_async(args: argparse.Namespace) -> None:
    if args.summarize_only:
        rows = [
            json.loads(line)
            for line in RESULTS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        print(f"저장된 원자료 재집계: {RESULTS_PATH.relative_to(_ROOT)} ({len(rows)}행)")
        summarize(rows)
        return

    today = date.today()
    cases = load_cases(limit=args.limit, blocks=args.blocks)
    print(
        f"케이스 {len(cases)}건 × 반복 {args.repeats}회 = 호출 {len(cases) * args.repeats}건"
        f"{' (dry-run)' if args.dry_run else ''}"
    )
    rows: list[dict[str, Any]] = []
    for repeat in range(args.repeats):
        for case in cases:
            row = await run_case(case, repeat, today=today, dry_run=args.dry_run)
            rows.append(row)
            print("!" if row.get("fell_back") else ".", end="", flush=True)
    print()
    if not args.dry_run:
        RESULTS_PATH.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
        print(f"원자료: {RESULTS_PATH.relative_to(_ROOT)} ({len(rows)}행)")
    summarize(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="L1-7B 1단계 — v3 검토기 단독 (실 LLM 호출)")
    parser.add_argument("--limit", type=int, default=None, help="앞 N건만 (스모크)")
    parser.add_argument("--repeats", type=int, default=1, help="케이스당 반복 횟수")
    parser.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 구성만 확인")
    parser.add_argument("--summarize-only", action="store_true", help="저장된 원자료만 다시 집계")
    parser.add_argument(
        "--blocks", nargs="*", default=None, help="블록 필터 (defect_free_control / seeded_defect)"
    )
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
