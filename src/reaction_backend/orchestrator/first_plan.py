"""First Plan Orchestrator (#32) — Sequential + 룰 fallback (ADR-0005 §2.5.1).

상태머신 (architecture.md §2.1):
    VALIDATING → PLANNING → REVIEWING → (HITL) → SAVING → DONE
                    ▲___________ feedback (≤2회) ___________│

흐름:

    validate_inputs → decompose_goal → schedule_blocks → review_plan ─┐
                            ▲                                          │ should_replan
                            └──────────── replan (≤2회) ───────────────┤
                                                          approve ─────┴→ END

- 입력은 Deep Interview(#6) 의 경계 계약 `InterviewOutcome` **하나**. InterviewState 를
  절대 import 하지 않는다 → 두 이슈 병렬 개발, 계약만 고정.
- LLM(②③④)은 Node 안 `aiClient.run(...)` 만. 스케줄링은 **룰만**(LLM 0회) —
  기존 `goal_structuring.py` 재사용(ADR-0005 §1.2). 8s timeout / rate limit → 룰 fallback.
- 산출물은 비활성 Draft. 실제 영속화는 사용자 [수락] 후 SAVING 단일 트랜잭션
  (`first_plan_adapter.db_apply_first_plan`) — AGENTS.md §1.4 자동 적용 금지.

본 파일은 **베이스라인 구조**다. LLM 프롬프트 연결·룰 스케줄러 통합·SAVING 트랜잭션의
세부 구현은 #32 본 구현 PR 에서 노드 본문을 채운다.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from reaction_backend.llm import aiClient
from reaction_backend.orchestrator import first_plan_adapter
from reaction_backend.orchestrator.goal_structuring import (
    compute_free_blocks,
    reserve_habit_sessions,
    time_policies_to_busy,
)
from reaction_backend.schemas.interview import InterviewOutcome
from reaction_backend.schemas.planning import (
    GoalDecomposition,
    GoalNodeDraft,
    PlanReview,
    ScheduledBlockPreview,
)

__all__ = [
    "FirstPlanState",
    "build_first_plan_graph",
    "initial_state",
    "should_replan",
]

MAX_REPLAN = 2  # Review feedback cycle 최대 2회, 3회째 그대로 HITL (무한 cycle 방지)


class FirstPlanState(TypedDict):
    """First Plan short-lived 상태. 입력은 InterviewOutcome 하나(경계 계약)."""

    user_id: UUID
    outcome: InterviewOutcome
    target_date: str  # "YYYY-MM-DD" (KST 기준)

    # VALIDATING
    missing_fields: list[str]
    tier_violation: str | None  # Focus≤3 / Maintain≤5 초과 (DevBaseline §1.4)

    # PLANNING
    planning_context: dict[str, Any]
    goal_plan: GoalDecomposition | None

    # PLANNING (룰 스케줄러 산출 — LLM 0회)
    scheduled_blocks: list[ScheduledBlockPreview]
    schedule_warnings: list[str]

    # REVIEWING
    review: PlanReview | None
    replan_count: int

    # 공통
    horizon: str | None
    used_fallback: bool  # 어느 LLM 노드든 fell_back 누적 → 응답 ai_source


def initial_state(*, user_id: UUID, outcome: InterviewOutcome, target_date: str) -> FirstPlanState:
    return FirstPlanState(
        user_id=user_id,
        outcome=outcome,
        target_date=target_date,
        missing_fields=[],
        tier_violation=None,
        planning_context={},
        goal_plan=None,
        scheduled_blocks=[],
        schedule_warnings=[],
        review=None,
        replan_count=0,
        horizon=outcome.horizon,
        used_fallback=False,
    )


def _session(config: RunnableConfig) -> Any:
    return config.get("configurable", {}).get("session")


def _tone_mode(config: RunnableConfig) -> str | None:
    """config["configurable"]["tone_mode"] 안전 추출 (#23-D)."""
    raw = config.get("configurable", {}).get("tone_mode")
    return raw if isinstance(raw, str) else None


# ─────────────────────────────────────────────────────────────────────────────
# 룰 fallback — 같은 schema 로 환원.
# ─────────────────────────────────────────────────────────────────────────────


def _rule_decomposition(state: FirstPlanState) -> GoalDecomposition:
    """LLM 분해 실패 시 룰: heaviest 목표 1개를 root leaf 로 환원 (#32 에서 정교화)."""
    return GoalDecomposition(
        goal_nodes=[
            GoalNodeDraft(
                node_id="tmp-root",
                parent_id=None,
                title=state["outcome"].core_goals[0].title,
                node_type="root",
                order_index=0,
                is_leaf=True,
            )
        ],
        action_items=[],
        policy_violations=[],
    )


def _rule_review(state: FirstPlanState) -> PlanReview:
    """리뷰 LLM 실패 시 룰: 그대로 승인(무한 cycle 방지, HITL 이 최종 게이트)."""
    return PlanReview(approved=True, feedback=[])


def _replan_feedback(state: FirstPlanState) -> str:
    """직전 REVIEWING 피드백 → decompose 프롬프트 변수(`{{review_feedback}}`).

    replan 엣지(review_plan → decompose_goal)로 재진입할 때 직전 리뷰의 미승인 사유를 실어,
    재분해가 **동일 결과를 반복하지 않고 실제로 다듬어지게** 한다(과거엔 같은 프롬프트를
    그대로 재실행해 cycle 이 무의미했다). 첫 분해(리뷰 이전)엔 피드백이 없으므로 빈 신호.
    """
    review = state.get("review")
    if review is None or not review.feedback:
        return "(첫 분해 — 이전 피드백 없음)"
    return "\n".join(f"- {item}" for item in review.feedback)


# ─────────────────────────────────────────────────────────────────────────────
# Nodes
# ─────────────────────────────────────────────────────────────────────────────


async def validate_inputs(state: FirstPlanState, config: RunnableConfig) -> FirstPlanState:
    """VALIDATING — 필수 슬롯 누락 + Focus/Maintain cap 검증.

    누락은 outcome.unresolved_slots 를 그대로 승계(인터뷰가 이미 결정적으로 계산).
    Focus ≤ 3 / Maintain ≤ 5 초과 시 tier_violation 기록 (DevBaseline §1.4 잠금) —
    라우터가 GOAL_TIER_LIMIT_EXCEEDED 422. 룰만(LLM 0회).
    """
    outcome = state["outcome"]
    focus_count = sum(1 for g in outcome.core_goals if g.tentative_tier == "focus")
    maintain_count = sum(1 for g in outcome.core_goals if g.tentative_tier == "maintain")
    if focus_count > 3:
        violation: str | None = "focus_cap_exceeded"
    elif maintain_count > 5:
        violation = "maintain_cap_exceeded"
    else:
        violation = None
    return {
        **state,
        "missing_fields": list(outcome.unresolved_slots),
        "tier_violation": violation,
        "planning_context": first_plan_adapter.context_from_outcome(outcome),
    }


async def decompose_goal(state: FirstPlanState, config: RunnableConfig) -> FirstPlanState:
    """PLANNING (LLM ②③) — goal_node 트리 + action_item 분해.

    replan 재진입 시엔 직전 리뷰 피드백(`_replan_feedback`)을 프롬프트에 실어, 재분해가
    같은 결과를 반복하지 않고 검토 지적을 반영하도록 닫힌 루프를 만든다.
    """
    ctx = state["planning_context"]
    prompt_vars = ctx.get("prompt_vars", {}) if isinstance(ctx, dict) else {}
    result = await aiClient.run(
        module="planning",
        schema=GoalDecomposition,
        prompt_id="planning/goal_decompose",
        fallback=lambda: _rule_decomposition(state),
        timeout=8.0,
        variables={**prompt_vars, "review_feedback": _replan_feedback(state)},
        user_id=state["user_id"],
        session=_session(config),
        tone_mode=_tone_mode(config),
    )
    return {
        **state,
        "goal_plan": result.value,
        "used_fallback": state["used_fallback"] or result.fell_back,
    }


async def schedule_blocks(state: FirstPlanState, config: RunnableConfig) -> FirstPlanState:
    """PLANNING (룰 only, LLM 0회) — goal_structuring.py free/busy + 배치 알고리즘 재사용.

    분해된 action_item 을 outcome 가용 시간(free/busy)에 실제로 배치한다.
    - `time_policies_to_busy` + `compute_free_blocks`: 활동 윈도우/노터치를 제외한 가용 구간.
    - `reserve_habit_sessions`: priority 오름차순 + 길이 맞는 가장 이른 free 슬롯 배치
      (분해 순서를 priority, estimated_minutes 를 길이로 환원 — `action_placements`).
    배치 못 한 항목은 `schedule_warnings` 로 남겨 REVIEWING 의 conflict_report 에 합류한다.
    산출물은 미영속 미리보기(`ScheduledBlockPreview`) — 자동 적용 금지(AGENTS §1.4).
    """
    gp = state["goal_plan"]
    action_items = list(gp.action_items) if gp is not None else []

    day = date.fromisoformat(state["target_date"])
    policies = first_plan_adapter.time_policies_from_outcome(state["outcome"])
    placements = first_plan_adapter.action_placements(action_items)
    node_by_placement = {p.id: getattr(p, "node_id", "") for p in placements}

    busy = time_policies_to_busy(day, policies)
    free = compute_free_blocks(day, busy)
    placed, _remaining = reserve_habit_sessions(day, free, placements)

    blocks = [
        ScheduledBlockPreview(
            start=b.interval.start,
            end=b.interval.end,
            title=b.title,
            category=b.category,
            origin="goal",
            origin_id=(node_by_placement.get(b.origin_id) or None)
            if b.origin_id is not None
            else None,
        )
        for b in placed
    ]
    placed_ids = {b.origin_id for b in placed}
    warnings = [
        f"'{p.title}' 을(를) 배치할 가용 시간을 찾지 못했어요. 다른 시간으로 옮겨볼까요?"
        for p in placements
        if p.id not in placed_ids
    ]
    return {**state, "scheduled_blocks": blocks, "schedule_warnings": warnings}


def _review_variables(state: FirstPlanState) -> dict[str, str]:
    """`planning/plan_quality` 프롬프트 변수 계약 (PR #44).

    goal_plan(분해 결과) + planning_context(요약) + policy_violations(충돌)를 프롬프트가
    요구하는 4종 문자열로 평탄화한다. 누락 시 render 실패 → 룰 fallback 으로 빠지므로
    리뷰 LLM 이 실제로 돌게 하려면 4종 모두 채워야 한다.
    """
    prompt_vars = state["planning_context"].get("prompt_vars", {})
    time_policy_summary = str(prompt_vars.get("time_policy_summary", ""))
    gp = state["goal_plan"]
    if gp is None:
        return {
            "goal_nodes_json": "[]",
            "action_items_json": "[]",
            "time_policy_summary": time_policy_summary,
            "conflict_report": "분해 결과 없음",
        }
    # 정책 위반(분해) + 룰 스케줄러 배치 실패(schedule_blocks)를 함께 검토 대상으로 전달.
    conflict_parts = [f"{v.node_id}: {v.reason}" for v in gp.policy_violations]
    conflict_parts.extend(state["schedule_warnings"])
    conflicts = "; ".join(conflict_parts) if conflict_parts else "충돌 없음"
    return {
        "goal_nodes_json": json.dumps([n.model_dump() for n in gp.goal_nodes], ensure_ascii=False),
        "action_items_json": json.dumps(
            [a.model_dump() for a in gp.action_items], ensure_ascii=False
        ),
        "time_policy_summary": time_policy_summary,
        "conflict_report": conflicts,
    }


async def review_plan(state: FirstPlanState, config: RunnableConfig) -> FirstPlanState:
    """REVIEWING (LLM ④) — 플랜 품질 독립 검토. 미승인 시 재계획 cycle.

    `planning/plan_quality` 변수 계약을 `_review_variables` 로 채운다 → 리뷰 LLM 실제 실행
    (과거 `variables={}` 는 render 실패로 항상 룰 승인 fallback 이었다).
    """
    result = await aiClient.run(
        module="planning",
        schema=PlanReview,
        prompt_id="planning/plan_quality",
        fallback=lambda: _rule_review(state),
        timeout=8.0,
        variables=_review_variables(state),
        user_id=state["user_id"],
        session=_session(config),
        tone_mode=_tone_mode(config),
    )
    return {
        **state,
        "review": result.value,
        "replan_count": state["replan_count"] + 1,
        "used_fallback": state["used_fallback"] or result.fell_back,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Conditional edge — 순수 함수.
# ─────────────────────────────────────────────────────────────────────────────


def should_replan(state: FirstPlanState) -> Literal["replan", "approve"]:
    """리뷰 미승인 + 재계획 한도(2회) 미도달이면 재계획, 아니면 HITL 로 진행."""
    review = state["review"]
    if review is not None and not review.approved and state["replan_count"] < MAX_REPLAN:
        return "replan"
    return "approve"


def build_first_plan_graph() -> CompiledStateGraph[
    FirstPlanState, Any, FirstPlanState, FirstPlanState
]:
    """Sequential + 룰 fallback StateGraph 컴파일."""
    graph = StateGraph(FirstPlanState)
    graph.add_node("validate_inputs", validate_inputs)
    graph.add_node("decompose_goal", decompose_goal)
    graph.add_node("schedule_blocks", schedule_blocks)
    graph.add_node("review_plan", review_plan)

    graph.set_entry_point("validate_inputs")
    graph.add_edge("validate_inputs", "decompose_goal")
    graph.add_edge("decompose_goal", "schedule_blocks")
    graph.add_edge("schedule_blocks", "review_plan")
    graph.add_conditional_edges(
        "review_plan",
        should_replan,
        {"replan": "decompose_goal", "approve": END},
    )
    return graph.compile()
