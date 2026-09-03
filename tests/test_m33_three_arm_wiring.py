"""M33 3-arm 배선 — **프로덕션 노드를 부르는가, 옮겨 적었는가.**

⚠️ 이 레포는 프롬프트·변수 계약을 **옮겨 적었다가** 34호출을 통째로 버린 적이 있다
(`l1-7-results.md` §5). 그래서 이 파일이 지키는 첫 번째는 **복사하지 않았다는 것**이다.

두 번째는 **B/C arm 이 실제로 다른 피드백을 보낸다**는 것이다. 스모크에서 세 케이스가
모두 승인이 나면 그 경로가 한 번도 안 밟히므로, 여기서 LLM 없이 직접 확인한다.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from scripts import l1_7_run as H
from scripts import l1_7b_m33_run as M

from reaction_backend.orchestrator import first_plan as FP
from reaction_backend.schemas.planning import PlanReview

_ROOT = Path(__file__).resolve().parent.parent
_TODAY = date(2026, 9, 3)


def _state() -> Any:
    case = next(c for c in H.load_cases() if c["case_id"] == "normal-toeic-near")
    return FP.initial_state(
        user_id=uuid.uuid4(),
        outcome=H.build_outcome(case, today=_TODAY),
        target_date=_TODAY.isoformat(),
    )


# ── 1. 옮겨 적지 않았다 ─────────────────────────────────────────────────────


def test_harness_calls_production_nodes_not_copies() -> None:
    """분해·검토·재분해 판정을 **프로덕션 함수로** 부른다."""
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    for fn in (
        "FP.validate_inputs",
        "FP.decompose_goal",
        "FP.review_plan",
        "FP.should_replan",
        "FP._replan_feedback",
        "FP.initial_state",
    ):
        assert fn in src, f"프로덕션 함수 `{fn}` 을 안 쓰고 있다"


def test_harness_does_not_copy_prompt_or_variables() -> None:
    """프롬프트 id·변수 계약을 하네스가 **다시 쓰지 않는다.**"""
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    # 이름 **언급**은 정당하다(왜 안 쓰는지 적으려면 불러야 한다). 막을 것은 **사용**이다.
    for banned in ("prompt_id=", "aiClient.run(", '"prompt_vars"', "variables="):
        assert banned not in src, f"`{banned}` 를 하네스가 다시 들고 있다 — 갈릴 자리다"


def test_harness_never_opens_a_db_session() -> None:
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    for banned in ("AsyncSession", "get_session", "_db_time_policies"):
        assert banned not in src


def test_production_source_is_untouched() -> None:
    """`src/` 를 안 바꾼다 — 이 배선의 전제다."""
    assert hasattr(FP, "initial_state") and hasattr(FP, "_replan_feedback")


# ── 2. B/C arm 이 **실제로 다른 피드백**을 보낸다 ───────────────────────────


def test_c_arm_sends_the_empty_signal_via_production_helper() -> None:
    """C arm 은 `review.feedback` 을 비워서 만든다 — **프로덕션 함수가 빈 신호를 낸다.**

    프롬프트를 따로 쓰지 않는 것이 이 설계의 핵심이다.
    """
    st = _state()
    st["review"] = PlanReview(approved=False, feedback=["세션이 너무 짧아요", "순서를 바꿔보면"])
    b_sent = FP._replan_feedback(st)

    c = dict(st)
    c["review"] = PlanReview(approved=False, feedback=[])
    c_sent = FP._replan_feedback(c)

    assert "세션이 너무 짧아요" in b_sent, "B 가 피드백 전문을 안 보낸다"
    assert c_sent == "(첫 분해 — 이전 피드백 없음)"
    assert b_sent != c_sent, "B·C 가 같은 것을 보내면 M34 가 성립하지 않는다"


def test_c_arm_keeps_the_rejection_verdict() -> None:
    """C 는 **피드백만** 비운다 — 반려 판정 자체는 B 와 같아야 한다."""
    st = _state()
    review = PlanReview(approved=False, feedback=["x"])
    st["review"] = review
    st["replan_count"] = 0
    c = dict(st)
    c["review"] = PlanReview(approved=review.approved, feedback=[])
    assert c["review"].approved is review.approved
    assert FP.should_replan(st) == FP.should_replan(c) == "replan"


def test_approved_case_makes_all_three_arms_identical() -> None:
    """승인이면 **재분해하지 않는다** — 프로덕션 `should_replan` 과 같은 흐름."""
    st = _state()
    st["review"] = PlanReview(approved=True, feedback=[])
    assert FP.should_replan(st) == "approve"


def test_rejected_path_produces_three_distinct_arm_entries() -> None:
    """반려 경로에서 세 arm 이 **각각 기록**된다 (분해는 스텁으로 대체 — LLM 없음)."""
    case = next(c for c in H.load_cases() if c["case_id"] == "normal-toeic-near")
    calls: list[str] = []

    async def fake_validate(state: Any, cfg: Any) -> Any:
        from reaction_backend.orchestrator import first_plan_adapter as A

        return {
            **state,
            "planning_context": A.context_from_outcome(
                state["outcome"], target_date=_TODAY, density=state["density"]
            ),
        }

    async def fake_decompose(state: Any, cfg: Any) -> Any:
        calls.append(FP._replan_feedback(state))
        return {**state, "goal_plan": _stub_plan(len(calls)), "used_fallback": False}

    async def fake_review(state: Any, cfg: Any) -> Any:
        return {
            **state,
            "review": PlanReview(approved=False, feedback=["더 잘게 나눠보면"]),
            "replan_count": 1,
        }

    orig = (FP.validate_inputs, FP.decompose_goal, FP.review_plan)
    FP.validate_inputs, FP.decompose_goal, FP.review_plan = (  # type: ignore[assignment]
        fake_validate,
        fake_decompose,
        fake_review,
    )
    try:
        row = asyncio.run(M.run_case(case, 0, today=_TODAY))
    finally:
        FP.validate_inputs, FP.decompose_goal, FP.review_plan = orig  # type: ignore[assignment]

    assert row["rejected"] is True
    assert set(row["plans"]) == set(M.ARMS)
    assert all(row["plans"][a] is not None for a in M.ARMS)
    # 분해는 3회 — 초기 1 + B 1 + C 1.
    assert len(calls) == 3
    assert calls[0] == "(첫 분해 — 이전 피드백 없음)"  # 초기
    assert "더 잘게 나눠보면" in calls[1]  # B
    assert calls[2] == "(첫 분해 — 이전 피드백 없음)"  # C — 빈 피드백
    assert row["b_feedback_sent"] != row["c_feedback_sent"]


def _stub_plan(n: int) -> Any:
    from reaction_backend.schemas.planning import (
        ActionItemDraft,
        GoalDecomposition,
        GoalNodeDraft,
    )

    return GoalDecomposition(
        goal_nodes=[
            GoalNodeDraft(
                node_id="root",
                parent_id=None,
                title="t",
                node_type="root",
                order_index=0,
                is_leaf=False,
            )
        ],
        action_items=[
            ActionItemDraft(
                node_id=f"l{n}",
                title=f"작업 {n}",
                category="career",
                estimated_minutes=50,
                first_step="교재 펴기",
            )
        ],
    )


# ── 3. 일반/도전을 섞지 않는다 ──────────────────────────────────────────────


def test_stratum_is_required() -> None:
    """`--stratum` 이 필수여야 두 표본이 한 파일에 안 섞인다 (설계 §3.4)."""
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    assert "required=True" in src
    assert 'choices=("general", "challenge")' in src


@pytest.mark.parametrize(("stratum", "n"), [("general", 34), ("challenge", 16)])
def test_each_stratum_loads_its_own_cases(stratum: str, n: int) -> None:
    assert len(M.load_stratum(stratum)) == n


def test_output_paths_are_separate_per_stratum() -> None:
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    assert 'f"l1_7b_m33_{args.stratum}.jsonl"' in src


# ── 4. 사전등록 상수가 코드에 박혀 있다 ─────────────────────────────────────


def test_bootstrap_constants_match_the_design() -> None:
    """설계 §4.2 가 **실행 전에** 고정한 값 — 코드에서 바꾸면 사전등록을 어긴다."""
    assert M.BOOTSTRAP_N == 10_000
    assert M.BOOTSTRAP_SEED == 42
    assert M.PRIMARY_REPEAT == 0
    assert M.ARMS == ("A_none", "B_feedback", "C_retry")
