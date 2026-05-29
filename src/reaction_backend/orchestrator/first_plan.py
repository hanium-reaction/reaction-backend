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

from typing import Any, Literal, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from reaction_backend.llm import aiClient
from reaction_backend.orchestrator import first_plan_adapter
from reaction_backend.schemas.interview import InterviewOutcome
from reaction_backend.schemas.planning import GoalDecomposition, GoalNodeDraft, PlanReview

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
        review=None,
        replan_count=0,
        horizon=outcome.horizon,
        used_fallback=False,
    )


def _session(config: RunnableConfig) -> Any:
    return config.get("configurable", {}).get("session")


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


# ─────────────────────────────────────────────────────────────────────────────
# Nodes
# ─────────────────────────────────────────────────────────────────────────────


async def validate_inputs(state: FirstPlanState, config: RunnableConfig) -> FirstPlanState:
    """VALIDATING — 필수 슬롯 누락 + Focus/Maintain cap 검증.

    누락은 outcome.unresolved_slots 를 그대로 승계(인터뷰가 이미 결정적으로 계산).
    Focus ≤ 3 초과 시 tier_violation 기록 (라우터가 GOAL_TIER_LIMIT_EXCEEDED 422).
    """
    outcome = state["outcome"]
    focus_count = sum(1 for g in outcome.core_goals if g.tentative_tier == "focus")
    violation = "focus_cap_exceeded" if focus_count > 3 else None
    return {
        **state,
        "missing_fields": list(outcome.unresolved_slots),
        "tier_violation": violation,
        "planning_context": first_plan_adapter.context_from_outcome(outcome),
    }


async def decompose_goal(state: FirstPlanState, config: RunnableConfig) -> FirstPlanState:
    """PLANNING (LLM ②③) — goal_node 트리 + action_item 분해."""
    ctx = state["planning_context"]
    prompt_vars = ctx.get("prompt_vars", {}) if isinstance(ctx, dict) else {}
    result = await aiClient.run(
        module="planning",
        schema=GoalDecomposition,
        prompt_id="planning/goal_decompose",
        fallback=lambda: _rule_decomposition(state),
        timeout=8.0,
        variables=prompt_vars,
        user_id=state["user_id"],
        session=_session(config),
    )
    return {
        **state,
        "goal_plan": result.value,
        "used_fallback": state["used_fallback"] or result.fell_back,
    }


async def schedule_blocks(state: FirstPlanState, config: RunnableConfig) -> FirstPlanState:
    """PLANNING (룰 only, LLM 0회) — goal_structuring.py free/busy + 습관 배치 재사용.

    베이스라인: 통과 노드. 실제 스케줄링은 #32 본 구현에서 goal_structuring 의
    GoalStructuringOrchestrator 를 호출해 DraftPlan 을 만든다(자동 적용 금지).
    """
    return state


async def review_plan(state: FirstPlanState, config: RunnableConfig) -> FirstPlanState:
    """REVIEWING (LLM ④) — 플랜 품질 독립 검토. 미승인 시 재계획 cycle."""
    result = await aiClient.run(
        module="planning",
        schema=PlanReview,
        prompt_id="planning/plan_quality",
        fallback=lambda: _rule_review(state),
        timeout=8.0,
        variables={},
        user_id=state["user_id"],
        session=_session(config),
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
