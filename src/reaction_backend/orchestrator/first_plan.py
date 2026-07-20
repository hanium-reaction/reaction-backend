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
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, Literal, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from reaction_backend.config import get_settings
from reaction_backend.llm import aiClient
from reaction_backend.orchestrator import first_plan_adapter
from reaction_backend.orchestrator.goal_structuring import (
    BusyBlock,
    TimeInterval,
    fixed_schedules_to_busy,
    time_policies_to_busy,
)
from reaction_backend.orchestrator.plan_scheduler import schedule_actions_multiday
from reaction_backend.repositories.fixed_schedule_repo import FixedScheduleRepo
from reaction_backend.repositories.scheduled_block_repo import ScheduledBlockRepo
from reaction_backend.repositories.time_policy_repo import TimePolicyRepo
from reaction_backend.schemas.common import KST, now_kst, to_kst
from reaction_backend.schemas.interview import InterviewOutcome
from reaction_backend.schemas.planning import (
    ActionItemDraft,
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
    # 배치 범위: "horizon"(기본, 마감까지 전 구간) | "week"(target_date 가 속한 달력 주만).
    scope: Literal["week", "horizon"]
    # 계획 분량(밀도) — light/standard/intense. decompose 프롬프트의 '주당 세션 수' 하한으로 전개.
    density: str

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


def initial_state(
    *,
    user_id: UUID,
    outcome: InterviewOutcome,
    target_date: str,
    scope: Literal["week", "horizon"] = "horizon",
    density: str = "standard",
) -> FirstPlanState:
    return FirstPlanState(
        user_id=user_id,
        outcome=outcome,
        target_date=target_date,
        scope=scope,
        density=density,
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
    """LLM 분해 실패 시 룰 폴백 — heaviest 목표를 density 만큼 균등 '회차' 세션으로 환원.

    Gemini 미가용이라 내용 분해는 못 하지만, 사용자가 고른 분량(density → 주당 세션 수)만큼
    회차 세션을 만들어 빈 계획으로 떨어지지 않게 한다. category 는 영속화(approve) 시
    `_normalize_category` 가 enum 으로 정규화한다.
    """
    goals = state["outcome"].core_goals
    heaviest = next((g for g in goals if g.is_heaviest), goals[0])
    # LLM 경로와 동일하게, 주당 가용 시간(weekly_hours)이 있으면 그 시간 기반으로 세션 수를 잡고
    # 없으면 density 프리셋으로 폴백 — 룰 폴백도 사용자의 실제 시간에 맞춘 분량을 낸다.
    session_count = first_plan_adapter.target_sessions_per_week(state["outcome"], state["density"])
    # 룰 폴백 세션 길이도 목표별 세션 길이(없으면 전역/기본)를 따른다 — 하드코딩 30분 대신.
    session_len = first_plan_adapter.session_min_for(state["outcome"])

    root = GoalNodeDraft(
        node_id="tmp-root",
        parent_id=None,
        title=heaviest.title,
        node_type="root",
        order_index=0,
        is_leaf=False,
    )
    nodes = [root]
    actions: list[ActionItemDraft] = []
    for i in range(session_count):
        leaf_id = f"tmp-leaf-{i}"
        label = f"{heaviest.title} {i + 1}회차"
        nodes.append(
            GoalNodeDraft(
                node_id=leaf_id,
                parent_id="tmp-root",
                title=label,
                node_type="leaf",
                order_index=i,
                is_leaf=True,
            )
        )
        actions.append(
            ActionItemDraft(
                node_id=leaf_id,
                title=label,
                estimated_minutes=session_len,
                category=heaviest.category,
                first_step="가장 쉬운 부분부터 5분만 시작하기",
            )
        )
    return GoalDecomposition(goal_nodes=nodes, action_items=actions, policy_violations=[])


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
        "planning_context": first_plan_adapter.context_from_outcome(
            outcome, density=state["density"]
        ),
    }


async def decompose_goal(state: FirstPlanState, config: RunnableConfig) -> FirstPlanState:
    """PLANNING (LLM ②③) — goal_node 트리 + action_item 분해.

    replan 재진입 시엔 직전 리뷰 피드백(`_replan_feedback`)을 프롬프트에 실어, 재분해가
    같은 결과를 반복하지 않고 검토 지적을 반영하도록 닫힌 루프를 만든다.
    """
    ctx = state["planning_context"]
    prompt_vars = ctx.get("prompt_vars", {}) if isinstance(ctx, dict) else {}
    settings = get_settings()
    result = await aiClient.run(
        module="planning",
        schema=GoalDecomposition,
        prompt_id="planning/goal_decompose",
        fallback=lambda: _rule_decomposition(state),
        timeout=settings.llm_planning_timeout_seconds,
        variables={**prompt_vars, "review_feedback": _replan_feedback(state)},
        user_id=state["user_id"],
        session=_session(config),
        tone_mode=_tone_mode(config),
        thinking_budget=settings.llm_planning_thinking_budget,
    )
    # 세션 길이·개수 결정적 보정 — LLM 이 목표별 세션 길이를 무시하고 너무 짧게(9분) 내거나 세션을
    # 과다 생성해도, 밴드로 가두고 주당 시간만큼으로 잘라 이번 주 분량이 weekly_hours 에 맞게
    # 한다(#per-goal). 목표별 입력이 없으면 no-op.
    goal_plan = result.value
    if goal_plan is not None and goal_plan.action_items:
        goal_plan = first_plan_adapter.shape_action_plan(
            state["outcome"], state["density"], goal_plan
        )
    return {
        **state,
        "goal_plan": goal_plan,
        "used_fallback": state["used_fallback"] or result.fell_back,
    }


def _schedule_end(start_day: date, horizon: str | None, scope: str) -> date:
    """배치 종료일(포함)을 scope 로 결정한다.

    - "week": target_date 가 속한 **달력 주의 일요일**까지 (마감이 더 이르면 마감으로 캡).
      주 중간에 시작하면 지난 날은 배치하지 않으므로 실질 범위는 [target_date, 일요일].
    - "horizon": 마감(horizon)까지 전 구간. 마감이 없으면 target_date 하루.
    """
    deadline = date.fromisoformat(horizon) if horizon else None
    if scope == "week":
        sunday = start_day + timedelta(days=6 - start_day.weekday())  # 월=0 → 그 주 일요일
        end = min(sunday, deadline) if deadline is not None else sunday
    else:
        end = deadline if deadline is not None else start_day
    return max(end, start_day)


async def _existing_busy_by_day(
    config: RunnableConfig,
    user_id: UUID,
    start_day: date,
    end_day: date,
    exclude_target_date: date | None = None,
) -> dict[date, list[BusyBlock]]:
    """승인된 `scheduled_blocks` 를 날짜별 busy 로 — 재계획 시 비파괴 fit-around 용.

    `exclude_target_date` 가 주어지면 그 날짜 승인 시 supersede 로 취소될 **자기 이전 First
    Plan** 산출물의 블록은 busy 에서 뺀다(#118) — 재생성 계획이 '곧 자기 손으로 비울'
    슬롯을 피해 나쁘게(또는 빈 채로) 배치되지 않게. 시작된 카드·user_edit·다른 날짜·다른
    소스(inbox/recovery) 블록은 그대로 busy(회피 유지). session 이 없으면 빈 dict.
    """
    session = _session(config)
    if session is None:
        return {}
    stale_ids: set[UUID] = set()
    if exclude_target_date is not None:
        stale_ids = await first_plan_adapter.superseded_card_ids(
            session, user_id=user_id, target_date=exclude_target_date
        )
    start_dt = datetime.combine(start_day, time(0, 0), tzinfo=KST)
    end_dt = datetime.combine(end_day + timedelta(days=1), time(0, 0), tzinfo=KST)
    rows = await ScheduledBlockRepo(session).list_busy_between(user_id, start_dt, end_dt)
    busy: dict[date, list[BusyBlock]] = defaultdict(list)
    for row in rows:
        if row.action_item_id in stale_ids:
            continue  # 승인 시 supersede 될 자기 이전 계획 — busy 로 세지 않음(#118)
        s, e = to_kst(row.start_at), to_kst(row.end_at)
        if e <= s:
            continue
        busy[s.date()].append(BusyBlock(TimeInterval(s, e), "scheduled_block", "기존 일정"))
    return busy


async def _db_time_policies(config: RunnableConfig, user_id: UUID) -> list[Any]:
    """활성 DB `time_policies`(S07, 온보딩 후 수정 포함) — outcome 스냅샷과 합쳐 busy 로.

    session 이 없으면(단위 테스트/시스템) 빈 리스트 → outcome 정책만 사용.
    """
    session = _session(config)
    if session is None:
        return []
    return list(await TimePolicyRepo(session).list_active(user_id))


async def _fixed_schedules(config: RunnableConfig, user_id: UUID) -> list[Any]:
    """활성 `fixed_schedules`(수업·알바, S05) — AI 블록이 그 위에 겹치지 않도록 busy 로.

    session 이 없으면 빈 리스트.
    """
    session = _session(config)
    if session is None:
        return []
    return list(await FixedScheduleRepo(session).list_active(user_id))


def _ceil_quarter(dt: datetime) -> datetime:
    """15분 경계로 올림 — 오늘 '지금' 이후 배치의 깔끔한 시작 경계."""
    dt = dt.replace(second=0, microsecond=0)
    rem = dt.minute % 15
    return dt if rem == 0 else dt + timedelta(minutes=15 - rem)


async def schedule_blocks(state: FirstPlanState, config: RunnableConfig) -> FirstPlanState:
    """PLANNING (룰 only, LLM 0회) — 분해 action_item 을 다일 스케줄러로 배치한다.

    이전에는 `reserve_habit_sessions`(습관 하루 1세션)를 재사용해 `target_date` 하루에
    모든 카드를 몰아넣었다(#calendar-cramming). 이제 `plan_scheduler.schedule_actions_multiday`
    로 배치 범위(`scope`)에 걸쳐 분산 배치한다:
    - scope="horizon"(기본): **마감까지** 전 구간 — 실행이 마감 전 여러 날에 분배된다.
      한 주가 지나면 주간 재계획이 실패·잔여를 반영해 이후를 다시 쓴다(후속).
    - scope="week": target_date 가 속한 **달력 주**만 (가벼운 단기 계획).
    - 하루 집중 총량 상한을 채우면 다음 날로 넘기고, 피크 시간대 free 를 먼저 쓰며,
      긴 카드는 focus_duration 세션으로 쪼개고 카드 사이 휴식(break_pattern)을 둔다.

    busy 는 (1) 수면/노터치(outcome + **DB `time_policies`**, 온보딩 후 수정 반영) +
    (2) **이미 승인된 `scheduled_blocks`** + (3) **`fixed_schedules`(수업·알바)** 를 모두 합친다
    → 기존·고정·수정된 일정 위에 겹쳐 잡지 않는다(비파괴 fit-around, HITL 승인 보존). (#112)
    단 **재생성**이면 승인 시 supersede 로 취소될 자기 같은-날짜 이전 계획은 busy 에서 뺀다
    (#118) — 그렇지 않으면 새 계획이 곧 비워질 슬롯을 피해 나쁘게 배치된다.

    배치 못 한 항목은 `schedule_warnings` 로 남겨 REVIEWING 의 conflict_report 에 합류한다.
    산출물은 미영속 미리보기(`ScheduledBlockPreview`) — 자동 적용 금지(AGENTS §1.4).
    """
    gp = state["goal_plan"]
    action_items = list(gp.action_items) if gp is not None else []
    outcome = state["outcome"]
    user_id = state["user_id"]

    start_day = date.fromisoformat(state["target_date"])
    schedule_end = _schedule_end(start_day, outcome.horizon, state["scope"])
    # 먼 마감 희석 방지(#weekly-rate): weekly_hours 는 '주당' rate 다. 이번 분해량(action 수)을
    # 주당 rate 로 담는 데 필요한 주 수만큼으로 배치 창을 좁힌다. 그러지 않으면 scope="horizon"
    # 기본값에서 ~1주치 세션이 먼 마감(예: 이번 학기) 전체에 균등 분산돼 이번 주가 텅 빈다.
    # 마감이 그보다 가까우면 _schedule_end 캡이 그대로 이겨(마감까지 몰기) 유지된다. 이후 주는
    # 주간 재계획이 채운다(비지속 초안이라 안전).
    if action_items:
        rate = first_plan_adapter.target_sessions_per_week(outcome, state["density"])
        weeks_needed = max(1, -(-len(action_items) // max(rate, 1)))
        density_end = start_day + timedelta(days=weeks_needed * 7 - 1)
        schedule_end = max(min(schedule_end, density_end), start_day)
    # 정책 = outcome 스냅샷 + DB 정책(온보딩 후 수정 포함). union → compute_free_blocks 가 병합.
    policies = [
        *first_plan_adapter.time_policies_from_outcome(outcome),
        *await _db_time_policies(config, user_id),
    ]
    fixed = await _fixed_schedules(config, user_id)
    actions = first_plan_adapter.plan_actions_from_decomposition(action_items)
    node_by_action = {a.id: a.node_id for a in actions}

    # exclude_target_date=start_day: 재생성 시 승인이 곧 supersede 할 자기 이전 계획을
    # busy 에서 제외 (#118). 첫 계획이면 교체 대상이 없어 no-op.
    existing_busy = await _existing_busy_by_day(
        config, user_id, start_day, schedule_end, exclude_target_date=start_day
    )

    # 오늘 계획을 저녁에 만들어도 이미 지난 시간대(예: 18:40 생성 → 12:00)에 세션이 잡히지
    # 않도록, '오늘'의 [00:00, 지금(15분 올림)) 구간을 busy 로 넣어 과거 배치를 막는다.
    now = now_kst()
    past_cutoff = _ceil_quarter(now)
    day_zero = datetime.combine(now.date(), time(0, 0), tzinfo=KST)

    def busy_for_day(day: date) -> list[BusyBlock]:
        extra: list[BusyBlock] = []
        if day == now.date() and past_cutoff > day_zero:
            extra.append(BusyBlock(TimeInterval(day_zero, past_cutoff), "past", "지난 시간"))
        return [
            *time_policies_to_busy(day, policies),
            *fixed_schedules_to_busy(day, fixed),
            *existing_busy.get(day, []),
            *extra,
        ]

    placed, warnings = schedule_actions_multiday(
        start_day=start_day,
        horizon_day=schedule_end,
        actions=actions,
        busy_for_day=busy_for_day,
        peak_windows=first_plan_adapter.peak_windows_from_outcome(outcome),
        focus_chunk_min=first_plan_adapter.focus_chunk_min_from_outcome(outcome),
        break_min=first_plan_adapter.break_min_from_outcome(outcome),
        daily_focus_cap_min=first_plan_adapter.daily_cap_for(state["density"]),
    )

    blocks = [
        ScheduledBlockPreview(
            start=b.interval.start,
            end=b.interval.end,
            title=b.title,
            category=b.category,
            origin="goal",
            origin_id=(node_by_action.get(b.origin_id) or None)
            if b.origin_id is not None
            else None,
        )
        for b in placed
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
    settings = get_settings()
    result = await aiClient.run(
        module="planning",
        schema=PlanReview,
        prompt_id="planning/plan_quality",
        fallback=lambda: _rule_review(state),
        timeout=settings.llm_planning_timeout_seconds,
        variables=_review_variables(state),
        user_id=state["user_id"],
        session=_session(config),
        tone_mode=_tone_mode(config),
        thinking_budget=settings.llm_planning_thinking_budget,
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
