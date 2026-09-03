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


def summarize(rows: list[dict[str, Any]], *, stratum: str) -> None:
    """⚠️ **M26-core 집계는 아직 붙이지 않았다.** 이 실행은 세 arm 의 계획을 만들고
    저장하는 데까지다 — 집계는 `l1_7_run.summarize_core` 를 붙이는 후속 작업이다.
    """
    ok = [r for r in rows if not r.get("fell_back")]
    fb = [r for r in rows if r.get("fell_back")]
    primary = [r for r in ok if r["repeat"] == PRIMARY_REPEAT]
    rej = [r for r in primary if r.get("rejected")]
    print(f"\n{'=' * 74}\nM33 3-arm [{stratum}] — 계획 생성 결과")
    print(f"실행 {len(rows)}행 / 집계 {len(ok)} / 폴백 {len(fb)}")
    if fb:
        print(f"  폴백 단계: {[r.get('stage') for r in fb][:6]}")
    print(f"\nrepeat {PRIMARY_REPEAT} 고유 {len(primary)}건 중 **반려 {len(rej)}건**")
    print("   ⚠️ 승인 케이스는 세 arm 이 같으므로 **ΔM26-core 에 0 을 기여**한다(설계 §1.1).")
    if rej:
        print(f"   반려: {', '.join(r['case_id'] for r in rej[:8])}")
        bad = [r for r in rej if not r["plans"].get("B_feedback") or not r["plans"].get("C_retry")]
        if bad:
            print(f"   ⚠️ 재분해가 폴백한 건 {len(bad)}건 — 집계에서 빼야 한다")
    print(
        "\n⚠️ **M26-core 집계는 아직 없다.** 이 실행은 세 arm 의 계획을 저장하는 데까지다.\n"
        f"⚠️ 일반/도전을 **섞지 않는다** — 이 파일은 [{stratum}] 전용이다.\n{'=' * 74}"
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
