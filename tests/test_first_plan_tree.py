"""첫 계획 초안의 **트리 정합성** — 모든 카드가 목표 트리의 노드에 매달린다 (planB-14).

LLM 이 트리에 없는 `node_id` 로 카드를 내면(미러 실측 `leaf_extra_1`) 승인 때
`goal_node_id` 없이 저장돼, 캘린더엔 있는데 목표 화면 단계 트리·진행률에서 빠졌다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from reaction_backend.llm.provider import ProviderResponse
from reaction_backend.orchestrator import first_plan, first_plan_adapter
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)
from reaction_backend.schemas.planning import ActionItemDraft, GoalDecomposition, GoalNodeDraft

KST = timezone(timedelta(hours=9))


def _node(node_id: str, parent: str | None, node_type: str, *, leaf: bool) -> GoalNodeDraft:
    return GoalNodeDraft(
        node_id=node_id,
        parent_id=parent,
        title=f"{node_id} 제목",
        node_type=node_type,  # type: ignore[arg-type]
        order_index=0,
        is_leaf=leaf,
    )


def _action(node_id: str, title: str) -> ActionItemDraft:
    return ActionItemDraft(
        node_id=node_id, title=title, estimated_minutes=40, category="study", first_step="펴기"
    )


def _plan_with_orphans() -> GoalDecomposition:
    return GoalDecomposition(
        goal_nodes=[
            _node("root", None, "root", leaf=False),
            _node("b1", "root", "branch", leaf=False),
            _node("leaf-1", "b1", "leaf", leaf=True),
        ],
        action_items=[
            _action("leaf-1", "LC 파트 1 사진 묘사 10문항"),
            _action("leaf_extra_1", "LC 파트 1, 2 빈출 전치사 정리"),
            _action("leaf_extra_2", "RC 파트 5 품사 문제 20개"),
            _action("leaf_extra_1", "LC 파트 2 응답 20문항"),
        ],
        policy_violations=[],
    )


def test_orphan_actions_get_a_leaf_under_the_root() -> None:
    fixed = first_plan_adapter.attach_orphan_actions(_plan_with_orphans())

    ids = {n.node_id: n for n in fixed.goal_nodes}
    assert all(a.node_id in ids for a in fixed.action_items)
    for orphan in ("leaf_extra_1", "leaf_extra_2"):
        node = ids[orphan]
        assert node.parent_id == "root"
        assert node.node_type == "leaf" and node.is_leaf
    # 같은 없는 id 를 두 카드가 가리켜도 노드는 하나 — 첫 카드 제목.
    assert sum(1 for n in fixed.goal_nodes if n.node_id == "leaf_extra_1") == 1
    assert ids["leaf_extra_1"].title == "LC 파트 1, 2 빈출 전치사 정리"
    # root 아래 기존 형제(b1) 뒤로 순서를 잇는다.
    assert ids["leaf_extra_1"].order_index == 1 and ids["leaf_extra_2"].order_index == 2
    # 카드는 하나도 버리지 않는다.
    assert len(fixed.action_items) == 4


def test_plan_without_orphans_is_returned_untouched() -> None:
    plan = GoalDecomposition(
        goal_nodes=[_node("root", None, "root", leaf=True)],
        action_items=[_action("root", "바로 root 에 매단 카드")],
        policy_violations=[],
    )
    assert first_plan_adapter.attach_orphan_actions(plan) is plan


def test_leaf_root_stops_being_a_leaf_once_it_gets_children() -> None:
    plan = GoalDecomposition(
        goal_nodes=[_node("root", None, "root", leaf=True)],
        action_items=[_action("ghost", "없는 노드 카드")],
        policy_violations=[],
    )
    fixed = first_plan_adapter.attach_orphan_actions(plan)
    root = next(n for n in fixed.goal_nodes if n.node_id == "root")
    assert root.is_leaf is False


def _outcome() -> InterviewOutcome:
    return InterviewOutcome(
        session_id="t",
        generated_at=datetime.now(KST),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대3", season="학기중"),
        core_goals=[
            GoalCandidate(
                title="토익 900점",
                category="study",
                is_heaviest=True,
                tentative_tier="focus",
                confidence=0.9,
                frequency_per_week=4,
                session_length_min=40,
            )
        ],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:00"), peak_window=["저녁"]
        ),
        preferences=PreferenceProfile(recovery_tone="담백", rest_ok=True, downscope_unit_min=10),
        horizon=None,
    )


async def test_decompose_hands_on_a_tree_that_holds_every_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """분해 노드를 지난 초안의 모든 카드가 트리 노드를 가리킨다 — 승인의 goal_node_id 연결 전제."""

    async def fake_generate_structured(**kwargs: object) -> tuple[BaseModel, ProviderResponse]:
        return _plan_with_orphans(), ProviderResponse(
            raw_text="{}", tokens_in=10, tokens_out=5, model="fake"
        )

    monkeypatch.setattr(
        "reaction_backend.llm.tool_executor.generate_structured", fake_generate_structured
    )
    target = date(2026, 9, 21)
    outcome = _outcome()
    state = first_plan.initial_state(
        user_id=uuid4(), outcome=outcome, target_date=target.isoformat()
    )
    state["planning_context"] = first_plan_adapter.context_from_outcome(outcome, target_date=target)

    config: Any = {"configurable": {}}
    new_state = await first_plan.decompose_goal(state, config)

    gp = new_state["goal_plan"]
    assert gp is not None and new_state["decompose_fallback_reason"] is None
    node_ids = {n.node_id for n in gp.goal_nodes}
    assert {"leaf_extra_1", "leaf_extra_2"} <= node_ids
    assert all(a.node_id in node_ids for a in gp.action_items)
