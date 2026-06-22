"""First Plan 경계 어댑터 (ADR-0005 §7.4 규약).

`InterviewOutcome`(경계 계약) → First Plan 오케스트레이터가 쓰는 컨텍스트로 변환한다.
순수 함수 — LLM/DB 무관.

- `context_from_outcome`: LLM 분해 프롬프트(`planning/goal_decompose`) 변수 + 룰
  스케줄러(`goal_structuring.GoalStructuringInput`) 조립에 쓸 요약 dict.
- `time_policies_from_outcome` / `action_placements`: 룰 스케줄러
  (`goal_structuring.py`) 가 free/busy 계산·배치에 그대로 쓰는 구조적 입력으로 환원.
  ORM 없이 Protocol(TimePolicyLike/HabitLike)만 만족시키므로 LLM/DB 무관.
- 실제 DB 영속화(`db_apply_first_plan`)는 사용자 [수락] 후 라우터/SAVING 노드에서만
  수행 (AGENTS.md §1.4 자동 적용 금지) — 본 베이스라인에서는 시그니처만 정의.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import (
    ACTION_CATEGORY_VALUES,
    ActionItem,
)
from reaction_backend.db.models.goal import GOAL_CATEGORY_VALUES, GOAL_TIER_VALUES, Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.orchestrator.goal_structuring import (
    DraftPlan,
    DraftScheduledBlock,
    HabitLike,
    PolicyViolationError,
    TimeInterval,
    TimePolicyLike,
    policy_guarded_transaction,
)
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.interview import InterviewOutcome
from reaction_backend.schemas.planning import (
    ActionItemDraft,
    GoalNodeDraft,
    ScheduledBlockPreview,
)

# GoalNodeDraft.node_type(root/branch/leaf, LLM) → goal_nodes.node_type enum(core/subgoal/.../leaf).
_NODE_TYPE_MAP = {"root": "core", "branch": "subgoal", "leaf": "leaf"}


def context_from_outcome(outcome: InterviewOutcome) -> dict[str, Any]:
    """InterviewOutcome → First Plan 컨텍스트 dict.

    LLM 프롬프트 변수는 모두 문자열로 평탄화한다(`prompts.registry` 의 {{var}} 치환 계약).
    availability / preferences 원본 객체도 함께 실어 룰 스케줄러 어댑터가 재사용.
    """
    goals = outcome.core_goals
    heaviest = next((g for g in goals if g.is_heaviest), goals[0])

    prompt_vars: dict[str, str] = {
        "goal_title": heaviest.title,
        "why_now": heaviest.why_now or "",
        "horizon": outcome.horizon or "",
        "behavioral_summary": _behavioral_summary(outcome),
        "time_policy_summary": _time_policy_summary(outcome),
        "freebusy_summary": "",  # 캘린더 freebusy 는 라우터가 로드해 채움(별도 IO)
    }

    return {
        "prompt_vars": prompt_vars,
        "core_goals": [g.model_dump() for g in goals],
        "availability": outcome.availability.model_dump(),
        "preferences": outcome.preferences.model_dump(),
        "horizon": outcome.horizon,
        "unresolved_slots": list(outcome.unresolved_slots),
    }


def _behavioral_summary(outcome: InterviewOutcome) -> str:
    p = outcome.preferences
    parts = [f"회복 톤: {p.recovery_tone}", f"휴식 제안 수용: {p.rest_ok}"]
    if p.focus_duration_min:
        parts.append(f"집중 지속: {p.focus_duration_min}분")
    if p.weekly_energy:
        parts.append(f"이번 주 컨디션: {p.weekly_energy}")
    return " / ".join(parts)


def _time_policy_summary(outcome: InterviewOutcome) -> str:
    a = outcome.availability
    parts = [f"활동: {a.activity_window.start}~{a.activity_window.end}"]
    if a.peak_window:
        parts.append(f"피크: {', '.join(a.peak_window)}")
    if a.no_touch_windows:
        parts.append(f"노터치: {len(a.no_touch_windows)}건")
    return " / ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# 룰 스케줄러 입력 어댑터 (schedule_blocks 노드용, LLM 0회)
#
# goal_structuring.py 의 free/busy 계산·배치 알고리즘은 ORM 모델이 아니라 구조적 타입
# (Protocol) 만 요구한다. InterviewOutcome 의 가용 시간/선호를 그 Protocol 을 만족하는
# 경량 dataclass 로 환원해 룰 스케줄러를 그대로 재사용한다 (ADR-0005 §1.2).
# ─────────────────────────────────────────────────────────────────────────────


# NOTE: TimePolicyLike/HabitLike Protocol 은 settable 속성을 요구하므로(ORM 모델이 만족하는
# 형태) frozen 으로 두지 않는다. 어댑터가 만든 뒤 변형하지 않으므로 사실상 불변으로 쓴다.
@dataclass(slots=True)
class _RuleTimePolicy:
    """`TimePolicyLike` 구조적 만족 — outcome 가용 시간을 busy 계산용 정책으로 환원."""

    policy_type: str
    payload: Mapping[str, Any]
    is_active: bool = True


@dataclass(slots=True)
class _ActionPlacement:
    """`HabitLike` 구조적 만족 — action_item 을 룰 스케줄러의 배치 단위로 환원.

    `reserve_habit_sessions` 가 priority_level 오름차순 + time_preference 윈도우로
    배치하므로, 분해 순서를 priority_level 로, estimated_minutes 를 세션 길이로 매핑한다.
    """

    id: uuid.UUID
    title: str
    category: str
    minutes_per_session: int
    time_preference: str
    priority_level: int
    # HabitLike 는 위 6개 필드만 요구. 배치 후 node_id 복원용 메타.
    node_id: str = field(default="", compare=False)


def time_policies_from_outcome(outcome: InterviewOutcome) -> list[TimePolicyLike]:
    """outcome 가용 시간 → 룰 스케줄러 busy 계산용 시간 정책 목록.

    - 활동 윈도우 **바깥** 을 수면(sleep)으로 환원한다(자정을 넘는 구간). 활동 시간만
      가용으로 남으므로 free/busy 계산의 기준이 된다.
    - no_touch 윈도우는 그대로 no_touch 정책으로 전개(요일 제한 포함).
    """
    a = outcome.availability
    policies: list[TimePolicyLike] = [
        _RuleTimePolicy(
            policy_type="sleep",
            payload={
                "start_time": a.activity_window.end,
                "end_time": a.activity_window.start,
            },
        )
    ]
    for nt in a.no_touch_windows:
        policies.append(
            _RuleTimePolicy(
                policy_type="no_touch",
                payload={
                    "start_time": nt.window.start,
                    "end_time": nt.window.end,
                    "days_of_week": list(nt.days_of_week),
                },
            )
        )
    return policies


def action_placements(action_items: list[ActionItemDraft]) -> list[HabitLike]:
    """분해된 action_item → 룰 스케줄러 배치 단위(`HabitLike`).

    분해 목록 순서를 priority_level(1=최우선)로, estimated_minutes 를 세션 길이로 매핑한다.
    배치 결과 블록의 `origin_id` 로 다시 node_id 를 복원할 수 있도록 `node_id` 를 싣는다.
    """
    placements: list[HabitLike] = []
    for index, item in enumerate(action_items):
        placements.append(
            _ActionPlacement(
                id=uuid.uuid4(),
                title=item.title,
                category=item.category,
                minutes_per_session=item.estimated_minutes,
                time_preference="anytime",
                priority_level=index + 1,
                node_id=item.node_id,
            )
        )
    return placements


# ─────────────────────────────────────────────────────────────────────────────
# SAVING — 사용자 [수락] 후 단일 가드 트랜잭션 영속화 (ADR-0005 §2.5.1 / AGENTS §1.4)
#
# HITL [수락] 이후에만 호출되는 단 하나의 영속화 경로. PR #30 의
# `policy_guarded_transaction` 을 재사용해 절대 시간 정책 위반 시 즉시 롤백한다.
# #62: goal/goal_node 트리(temp_uuid → 실 UUID) + action_item 링크 + scheduled_blocks 까지
# 단일 트랜잭션 영속화 + 3회 재시도. ⚠️ dependency_links 는 GoalDecomposition 에 소스 데이터가
# 없어 후속 분리(이슈 #62 제외 범위).
# ─────────────────────────────────────────────────────────────────────────────

MAX_SAVE_RETRIES = 3  # ADR-0005 §2.5.1 — DB Agent 최대 3회 재시도 후 PLAN_SAVE_FAILED.


@dataclass(frozen=True, slots=True)
class FirstPlanSaveResult:
    """SAVING 영속화 결과 카운트."""

    goals: int
    goal_nodes: int
    action_items: int
    scheduled_blocks: int


def _normalize_category(raw: str) -> str:
    """ActionItem.category enum 으로 정규화 — 미지원 카테고리는 'other'."""
    return raw if raw in ACTION_CATEGORY_VALUES else "other"


def _normalize_goal_category(raw: str) -> str:
    return raw if raw in GOAL_CATEGORY_VALUES else "other"


def _normalize_goal_tier(raw: str) -> str:
    return raw if raw in GOAL_TIER_VALUES else "maintain"


def _node_depths(goal_nodes: Sequence[GoalNodeDraft]) -> dict[str, int]:
    """temp node_id → depth (parent_id 체인 hop 수). root = 0."""
    parent_of = {n.node_id: n.parent_id for n in goal_nodes}
    depths: dict[str, int] = {}
    for node in goal_nodes:
        depth = 0
        cursor = node.parent_id
        seen: set[str] = set()
        while cursor is not None and cursor in parent_of and cursor not in seen:
            seen.add(cursor)
            depth += 1
            cursor = parent_of[cursor]
        depths[node.node_id] = depth
    return depths


async def _apply_once(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    target_date: date,
    outcome: InterviewOutcome,
    goal_nodes: Sequence[GoalNodeDraft],
    action_items: Sequence[ActionItemDraft],
    blocks: Sequence[ScheduledBlockPreview],
    time_policies: Sequence[TimePolicyLike],
) -> FirstPlanSaveResult:
    """단일 가드 트랜잭션 1회 시도 — goals → goal_nodes → action_items → scheduled_blocks."""
    guard_plan = DraftPlan(
        target_date=target_date,
        blocks=tuple(
            DraftScheduledBlock(
                interval=TimeInterval(b.start, b.end),
                origin=b.origin,
                origin_id=None,
                title=b.title,
                category=b.category,
            )
            for b in blocks
        ),
        free_blocks=(),
        busy_blocks=(),
        warnings=(),
        generated_at=now_kst(),
    )

    async with policy_guarded_transaction(session, guard_plan, time_policies):
        # 1) goals — core_goals 전체 영속화. heaviest 가 분해 트리의 소속 goal.
        goal_rows: list[Goal] = []
        heaviest: Goal | None = None
        for gc in outcome.core_goals:
            g = Goal()
            g.user_id = user_id
            g.title = gc.title
            g.category = _normalize_goal_category(gc.category)
            g.goal_tier = _normalize_goal_tier(gc.tentative_tier)
            g.deadline = date.fromisoformat(gc.deadline) if gc.deadline else None
            g.status = "active"
            g.why_now = gc.why_now
            session.add(g)
            goal_rows.append(g)
            if gc.is_heaviest and heaviest is None:
                heaviest = g
        if heaviest is None and goal_rows:
            heaviest = goal_rows[0]
        await session.flush()  # goal.id 확보

        # 2) goal_nodes — heaviest goal 트리. temp node_id → GoalNode (parent 는 relationship).
        depths = _node_depths(goal_nodes)
        node_by_temp: dict[str, GoalNode] = {}
        for nd in goal_nodes:
            n = GoalNode()
            if heaviest is not None:
                n.goal_id = heaviest.id
            n.title = nd.title
            n.node_type = _NODE_TYPE_MAP.get(nd.node_type, "subgoal")
            n.depth = depths.get(nd.node_id, 0)
            n.order_index = nd.order_index
            n.is_leaf = nd.is_leaf
            session.add(n)
            node_by_temp[nd.node_id] = n
        for nd in goal_nodes:
            if nd.parent_id is not None and nd.parent_id in node_by_temp:
                node_by_temp[nd.node_id].parent = node_by_temp[nd.parent_id]
        await session.flush()  # goal_node.id 확보 (action_item FK)

        # 3) action_items — goal_id + goal_node_id 링크 (#62)
        action_by_node: dict[str, ActionItem] = {}
        for item in action_items:
            row = ActionItem()
            row.user_id = user_id
            row.title = item.title
            row.target_date = target_date
            row.estimated_minutes = item.estimated_minutes
            row.category = _normalize_category(item.category)
            row.status = "planned"  # 신규 카드 — 원본 status 변경 아님(AGENTS §2)
            row.source = "goal"
            row.first_step = item.first_step
            if heaviest is not None:
                row.goal_id = heaviest.id
            node = node_by_temp.get(item.node_id)
            if node is not None:
                row.goal_node_id = node.id
            session.add(row)
            action_by_node[item.node_id] = row
        await session.flush()  # action_item.id 확보 (block FK)

        # 4) scheduled_blocks — action_item 에 연결
        block_count = 0
        for b in blocks:
            action = action_by_node.get(b.origin_id or "")
            if action is None:
                continue  # node 에 매달리지 않은 block 은 영속 대상 아님(habit 등은 별도 경로)
            sb = ScheduledBlock()
            sb.user_id = user_id
            sb.action_item_id = action.id
            sb.start_at = b.start
            sb.end_at = b.end
            sb.source = "ai_plan"
            sb.block_status = "scheduled"
            session.add(sb)
            block_count += 1
        # dependency_links: GoalDecomposition 에 의존성 소스 데이터 없음 → 후속(#62 제외 범위).

    return FirstPlanSaveResult(
        goals=len(goal_rows),
        goal_nodes=len(goal_nodes),
        action_items=len(action_by_node),
        scheduled_blocks=block_count,
    )


async def db_apply_first_plan(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    target_date: date,
    outcome: InterviewOutcome,
    goal_nodes: Sequence[GoalNodeDraft],
    action_items: Sequence[ActionItemDraft],
    blocks: Sequence[ScheduledBlockPreview],
    time_policies: Sequence[TimePolicyLike],
    max_retries: int = MAX_SAVE_RETRIES,
) -> FirstPlanSaveResult:
    """승인된 Draft 를 goal 트리까지 단일 트랜잭션 영속화 + 최대 3회 재시도(ADR-0005 §2.5.1).

    정책 위반(`PolicyViolationError`)은 결정적이라 재시도하지 않고 즉시 전파한다. 그 외
    영속화 예외(IntegrityError 등)는 가드 트랜잭션이 롤백 후, 최대 `max_retries` 회 재시도하고
    마지막 실패는 원 예외를 전파한다(라우터가 `PLAN_SAVE_FAILED` 로 매핑).

    Raises:
        PolicyViolationError: block 이 절대 시간 정책(수면/노터치 등)을 침범한 경우.
        Exception: max_retries 회 모두 실패한 경우 마지막 예외.
    """
    last_exc: Exception | None = None
    for _attempt in range(max_retries):
        try:
            return await _apply_once(
                session,
                user_id=user_id,
                target_date=target_date,
                outcome=outcome,
                goal_nodes=goal_nodes,
                action_items=action_items,
                blocks=blocks,
                time_policies=time_policies,
            )
        except PolicyViolationError:
            raise  # 결정적 — 재시도 무의미
        except Exception as exc:  # noqa: BLE001 — 롤백은 가드 트랜잭션이 보장, 재시도 후 전파
            last_exc = exc
    assert last_exc is not None
    raise last_exc
