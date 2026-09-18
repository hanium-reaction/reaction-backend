"""첫 계획이 **사용자가 쓴 말**을 망가뜨리지 않는다 (planB 묶음).

금지어 필터는 LLM 출력 전체에 걸린다. 분해·마일스톤은 사용자 목표 제목을 그대로 옮겨 쓰므로,
필터가 사용자 원문까지 치환하면 목표 화면에 깨진 문장이 저장된다(planB-3). 여기서는
provider 만 가짜로 두고 `aiClient.run` 의 실제 필터 경로를 그대로 통과시킨다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from reaction_backend.llm.provider import ProviderResponse
from reaction_backend.orchestrator import first_plan, first_plan_adapter, first_plan_milestones
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)
from reaction_backend.schemas.planning import (
    GoalDecomposition,
    MilestoneDraft,
    MilestonePlan,
)

KST = timezone(timedelta(hours=9))
TITLE = "포기하지 않고 영어 회화 끝내기"
BROKEN = "잠깐 쉬어가는하지 않고 영어 회화 끝내기"


def _outcome(title: str = TITLE, **goal: Any) -> InterviewOutcome:
    heaviest = GoalCandidate(
        title=title,
        category="study",
        is_heaviest=True,
        tentative_tier="focus",
        confidence=0.9,
        frequency_per_week=3,
        session_length_min=40,
        **goal,
    )
    return InterviewOutcome(
        session_id="t",
        generated_at=datetime.now(KST),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대3", season="학기중"),
        core_goals=[heaviest],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:00"), peak_window=["저녁"]
        ),
        preferences=PreferenceProfile(recovery_tone="담백", rest_ok=True, downscope_unit_min=10),
        horizon=None,
    )


def _state(
    outcome: InterviewOutcome, milestones: list[MilestoneDraft] | None = None
) -> first_plan.FirstPlanState:
    target = date(2026, 9, 21)
    state = first_plan.initial_state(
        user_id=uuid4(),
        outcome=outcome,
        target_date=target.isoformat(),
        milestones=milestones,
    )
    state["planning_context"] = first_plan_adapter.context_from_outcome(outcome, target_date=target)
    return state


def _patch_provider(monkeypatch: pytest.MonkeyPatch, value: BaseModel | None) -> None:
    """provider 만 가짜로 — 금지어 필터·검증 등 `aiClient.run` 의 나머지는 실제로 돈다.

    `value` 가 None 이면 provider 가 실패해 룰 폴백으로 간다.
    """

    async def fake_generate_structured(**kwargs: object) -> tuple[BaseModel, ProviderResponse]:
        if value is None:
            raise TimeoutError("provider hung")
        return value, ProviderResponse(raw_text="{}", tokens_in=10, tokens_out=5, model="fake")

    monkeypatch.setattr(
        "reaction_backend.llm.tool_executor.generate_structured", fake_generate_structured
    )


def _llm_plan(root_title: str) -> GoalDecomposition:
    return GoalDecomposition(
        goal_nodes=[
            {
                "node_id": "root",
                "parent_id": None,
                "title": root_title,
                "node_type": "root",
                "order_index": 0,
                "is_leaf": False,
            },
            {
                "node_id": "leaf-1",
                "parent_id": "root",
                "title": "기초 표현 익히기",
                "node_type": "leaf",
                "order_index": 0,
                "is_leaf": True,
            },
        ],
        action_items=[
            {
                "node_id": "leaf-1",
                "title": f"{root_title} — 기초 표현 20개",
                "estimated_minutes": 40,
                "category": "study",
                # LLM 이 스스로 쓴 문장 — 이건 여전히 치환돼야 한다.
                "first_step": "포기하지 마세요, 표현 3개만 소리 내 읽기",
            }
        ],
        policy_violations=[],
    )


# ── planB-3 금지어 치환이 사용자 목표 제목을 깨뜨리지 않는다 ─────────────────────


async def test_decompose_keeps_users_goal_title_but_filters_llm_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """root 가 사용자 제목 그대로 남고, LLM 이 쓴 '포기하지 마세요' 는 여전히 치환된다."""
    _patch_provider(monkeypatch, _llm_plan(TITLE))

    new_state = await first_plan.decompose_goal(_state(_outcome()), {"configurable": {}})

    gp = new_state["goal_plan"]
    assert gp is not None
    assert new_state["decompose_fallback_reason"] is None
    root = next(n for n in gp.goal_nodes if n.node_type == "root")
    assert root.title == TITLE
    assert all(BROKEN not in n.title for n in gp.goal_nodes)
    assert all(BROKEN not in a.title for a in gp.action_items)
    first = gp.action_items[0]
    assert first.title.startswith(TITLE)
    # 필터는 꺼지지 않았다 — LLM 이 스스로 쓴 금지어는 치환된다.
    assert "포기하지 마세요" not in first.first_step
    assert "잠깐 쉬어가는" in first.first_step


async def test_rule_fallback_labels_keep_users_goal_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """분해가 폴백해도 '{제목} N회차' 가 원래 제목을 그대로 쓴다."""
    _patch_provider(monkeypatch, None)

    new_state = await first_plan.decompose_goal(_state(_outcome()), {"configurable": {}})

    gp = new_state["goal_plan"]
    assert gp is not None
    assert new_state["decompose_fallback_reason"] is not None
    assert gp.action_items
    assert all(a.title.startswith(f"{TITLE} ") for a in gp.action_items)
    assert all(BROKEN not in n.title for n in gp.goal_nodes)


async def test_user_edited_milestone_title_is_not_rewritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """사용자가 확정한 마일스톤 제목이 치환되면 branch 가 확정 목록과 어긋나 '빠졌다' 고 오고지한다."""
    milestone = "실패 원인 정리하고 다시 말하기"
    plan = GoalDecomposition(
        goal_nodes=[
            {
                "node_id": "root",
                "parent_id": None,
                "title": "영어 회화",
                "node_type": "root",
                "order_index": 0,
                "is_leaf": False,
            },
            {
                "node_id": "b1",
                "parent_id": "root",
                "title": milestone,
                "node_type": "branch",
                "order_index": 0,
                "is_leaf": False,
            },
            {
                "node_id": "leaf-1",
                "parent_id": "b1",
                "title": "틀린 문장 모으기",
                "node_type": "leaf",
                "order_index": 0,
                "is_leaf": True,
            },
        ],
        action_items=[
            {
                "node_id": "leaf-1",
                "title": "틀린 문장 5개 모으기",
                "estimated_minutes": 40,
                "category": "study",
                "first_step": "노트 펴기",
            }
        ],
        policy_violations=[],
    )
    _patch_provider(monkeypatch, plan)

    state = _state(_outcome("영어 회화"), milestones=[MilestoneDraft(title=milestone)])
    new_state = await first_plan.decompose_goal(state, {"configurable": {}})

    gp = new_state["goal_plan"]
    assert gp is not None
    assert any(n.title == milestone for n in gp.goal_nodes)
    assert first_plan_adapter.missing_milestone_titles([MilestoneDraft(title=milestone)], gp) == []


async def test_milestone_fallback_keeps_users_title_and_success_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage A 룰 폴백도 '{제목} 준비·기초' 와 완료 기준을 원문 그대로 싣는다."""
    _patch_provider(monkeypatch, None)
    outcome = _outcome(success_image="실패 걱정 없이 10분 대화하기")

    milestones, fell_back = await first_plan_milestones.generate_milestones(outcome=outcome)

    assert fell_back is True
    assert milestones[0].title == f"{TITLE} 준비·기초"
    assert milestones[-1].summary == "실패 걱정 없이 10분 대화하기"


async def test_milestone_llm_text_is_still_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    """사용자 원문이 아닌 LLM 문장의 금지어는 그대로 치환된다 — 필터 우회가 아니다."""
    _patch_provider(
        monkeypatch,
        MilestonePlan(
            milestones=[
                MilestoneDraft(title=f"{TITLE} 기초", summary="포기하고 싶을 때 쓸 루틴 만들기"),
            ]
        ),
    )

    milestones, fell_back = await first_plan_milestones.generate_milestones(outcome=_outcome())

    assert fell_back is False
    assert milestones[0].title == f"{TITLE} 기초"
    assert "포기하고" not in milestones[0].summary
