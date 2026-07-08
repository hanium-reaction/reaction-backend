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
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
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
from reaction_backend.orchestrator.interview_adapter import is_placeholder_goal
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.interview import GoalCandidate, InterviewOutcome
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

    # 시간 배치·일정 충돌은 룰 스케줄러(schedule_blocks)가 전담하므로 decompose 프롬프트에
    # freebusy 를 싣지 않는다 (과거 "" 빈 값이라 LLM 에 무의미했다). review_feedback 은
    # 재분해(replan) 시 first_plan.decompose_goal 이 직전 리뷰 피드백으로 채운다.
    prompt_vars: dict[str, str] = {
        "goal_title": heaviest.title,
        "why_now": heaviest.why_now or "",
        "horizon": outcome.horizon or "",
        "behavioral_summary": _behavioral_summary(outcome),
        "time_policy_summary": _time_policy_summary(outcome),
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


def _replaceable_action(action: ActionItem, target_date: date) -> bool:
    """이전 AI 계획 산출물 중 '사용자가 손대지 않은' 교체 대상인지.

    source='goal'(계획 분해 산출) + status='planned'(시작/체크인 이력 없음) + 미보관 +
    같은 target_date 만 교체한다. 시작·완료·실패 카드와 inbox/manual/recovery 카드는
    이력·사용자 의도 보존을 위해 남긴다 (AGENTS §2 원본 status 불변 원칙과 일관).
    """
    return (
        action.source == "goal"
        and action.status == "planned"
        and action.archived_at is None
        and action.target_date == target_date
    )


async def supersede_previous_plan(
    session: AsyncSession, *, user_id: uuid.UUID, target_date: date
) -> int:
    """같은 날짜의 이전 First Plan 산출물을 정리(soft) — 승인 = "이 계획으로 교체".

    generate 는 기존 블록을 busy 로 보지 않고(후속: 스케줄러 DB busy 통합 이슈) approve 는
    무조건 INSERT 만 해서, 재생성→재승인을 반복하면 같은 날짜에 카드/블록이 계속 누적됐다
    (같은 제목 ×5, 같은 시각 4중첩). 승인 시점에 같은 target_date 의 이전 AI 계획 산출물 중
    사용자가 손대지 않은 것만 정리해 "마지막 승인 = 그 날짜의 계획"이 되게 한다.

    "손대지 않은" 판정은 두 층이다:
    - 카드 층: `_replaceable_action` (source=goal · status=planned · 미보관 · 같은 날짜)
    - 블록 층: 카드의 블록 중 `source='user_edit'`(S15 직접 이동)가 하나라도 있으면
      그 카드는 **통째로 보존** — 사용자가 시간을 옮긴 계획을 승인이 지우면 안 된다.

    hard delete 금지(AGENTS §2) — action_item 은 archived_at(soft delete),
    scheduled_block 은 block_status='cancelled' 로 마킹한다. 반환값은 교체된 카드 수.

    카드 SELECT 는 FOR UPDATE — 같은 카드를 동시에 [시작]하는 요청(today/start)이
    status 를 in_progress 로 바꾸는 것과 교차해 '보관됐는데 실행 중'인 유령 카드가
    생기지 않게 행 잠금으로 직렬화한다. SQL WHERE 로 좁히고 파이썬 술어로 한 번 더
    거른다 — WHERE 를 평가하지 않는 구조적 fake session(테스트)에서도 규칙 유지.
    """
    stmt = (
        select(ActionItem)
        .where(
            ActionItem.user_id == user_id,
            ActionItem.target_date == target_date,
            ActionItem.source == "goal",
            ActionItem.status == "planned",
            ActionItem.archived_at.is_(None),
        )
        .with_for_update()
    )
    rows = (await session.execute(stmt)).scalars().all()
    candidates = [a for a in rows if _replaceable_action(a, target_date)]
    if not candidates:
        return 0

    candidate_ids = {a.id for a in candidates}
    block_stmt = select(ScheduledBlock).where(
        ScheduledBlock.user_id == user_id,
        ScheduledBlock.action_item_id.in_(candidate_ids),
        ScheduledBlock.block_status != "cancelled",
    )
    fetched = (await session.execute(block_stmt)).scalars().all()
    live_blocks = [
        b for b in fetched if b.action_item_id in candidate_ids and b.block_status != "cancelled"
    ]
    # 사용자가 직접 옮긴(user_edit) 블록을 가진 카드는 교체 대상에서 제외.
    protected_ids = {b.action_item_id for b in live_blocks if b.source == "user_edit"}
    stale = [a for a in candidates if a.id not in protected_ids]
    if not stale:
        return 0

    archived_at = now_kst()
    stale_ids = {a.id for a in stale}
    for action in stale:
        action.archived_at = archived_at
    for block in live_blocks:
        if block.action_item_id in stale_ids:
            block.block_status = "cancelled"
    return len(stale)


async def _archive_goal_nodes(session: AsyncSession, *, goal_id: uuid.UUID) -> int:
    """goal 의 기존 활성 분해 트리를 보관 — 새 승인 트리가 '현재 트리'가 되게.

    매 승인이 heaviest goal 아래에 goal_nodes 트리를 새로 INSERT 하므로, 이전 트리를
    archived_at 으로 보관하지 않으면 승인 반복 시 동일 트리가 무한 누적된다(카드/블록과
    같은 뿌리의 세 번째 테이블). 보관된 노드를 가리키는 기존 action_item 의
    goal_node_id 는 계보(lineage)로 유지된다. 반환값은 보관한 노드 수.
    """
    stmt = select(GoalNode).where(
        GoalNode.goal_id == goal_id,
        GoalNode.archived_at.is_(None),
    )
    rows = (await session.execute(stmt)).scalars().all()
    stale = [n for n in rows if n.goal_id == goal_id and n.archived_at is None]
    archived_at = now_kst()
    for node in stale:
        node.archived_at = archived_at
    return len(stale)


def _normalize_category(raw: str) -> str:
    """ActionItem.category enum 으로 정규화 — 미지원 카테고리는 'other'."""
    return raw if raw in ACTION_CATEGORY_VALUES else "other"


def _normalize_goal_category(raw: str) -> str:
    return raw if raw in GOAL_CATEGORY_VALUES else "other"


def _derive_goal_category(action_categories: Sequence[str]) -> str | None:
    """액션 카테고리 다수결로 목표 카테고리 파생 — 전부 'other'거나 비면 None.

    인터뷰는 목표 카테고리를 분류하지 않아 'other' 로 저장되므로
    (interview_adapter._build_goals), 분해된 액션들의 실카테고리에서 역산한다.
    ACTION_CATEGORY_VALUES ⊆ GOAL_CATEGORY_VALUES (동일 enum) 이라 그대로 대입 가능.
    """
    counts = Counter(c for c in action_categories if c != "other")
    if not counts:
        return None
    return counts.most_common(1)[0][0]


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


async def _active_goals(session: AsyncSession, user_id: uuid.UUID) -> list[Goal]:
    stmt = select(Goal).where(Goal.user_id == user_id, Goal.archived_at.is_(None))
    return list((await session.execute(stmt)).scalars().all())


async def materialize_goals(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    core_goals: Sequence[GoalCandidate],
) -> tuple[list[Goal], Goal | None]:
    """core_goals → 영속 Goal 목록 + heaviest. 이미 있는 제목은 재사용(중복 생성 방지).

    딥 인터뷰 완료(#96)와 계획 승인(#62)이 공유한다: 인터뷰가 먼저 목표를 저장해
    분류 화면(GET /goals)에 노출·재분류할 수 있게 하고, 이후 계획 승인은 같은 목표를
    **재사용**(신규 생성 X)해 중복을 막는다. 미입력 placeholder(#88)는 제외.
    """
    existing = {g.title: g for g in await _active_goals(session, user_id)}
    goal_rows: list[Goal] = []
    heaviest: Goal | None = None
    for gc in core_goals:
        if is_placeholder_goal(gc):
            continue
        g = existing.get(gc.title)
        if g is None:
            g = Goal()
            g.user_id = user_id
            g.title = gc.title
            g.category = _normalize_goal_category(gc.category)
            g.goal_tier = _normalize_goal_tier(gc.tentative_tier)
            g.deadline = date.fromisoformat(gc.deadline) if gc.deadline else None
            g.status = "active"
            g.why_now = gc.why_now
            session.add(g)
            existing[gc.title] = g
        goal_rows.append(g)
        if gc.is_heaviest and heaviest is None:
            heaviest = g
    if heaviest is None and goal_rows:
        heaviest = goal_rows[0]
    await session.flush()
    return goal_rows, heaviest


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
    on_success: Callable[[], Awaitable[None]] | None = None,
) -> FirstPlanSaveResult:
    """단일 가드 트랜잭션 1회 시도 — goals → goal_nodes → action_items → scheduled_blocks.

    `on_success` 는 영속화 직후 **같은 가드 트랜잭션 안**에서 호출된다 — 호출자가
    Draft 상태 전이 등 부수 기록을 계획 영속화와 원자적으로(단일 commit) 묶을 수 있게.
    실패 시 롤백에 함께 쓸려 나간다.
    """
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
        # 1) goals — 인터뷰 완료 시 이미 저장된 목표를 재사용(중복 방지, #96), placeholder 제외(#88).
        #    heaviest 가 분해 트리의 소속 goal.
        goal_rows, heaviest = await materialize_goals(
            session, user_id=user_id, core_goals=outcome.core_goals
        )

        # 실제 목표가 없으면(=goals.list 미입력) 트리/액션도 만들지 않는다: placeholder 로부터
        # 분해된 노드는 소속시킬 goal 이 없고(GoalNode.goal_id 는 NOT NULL) 의미도 없다.
        node_by_temp: dict[str, GoalNode] = {}
        action_by_node: dict[str, ActionItem] = {}
        block_count = 0
        if heaviest is None:
            # 빈 계획도 승인 자체는 성립 — 부수 기록(Draft 승인 등)은 같은 트랜잭션으로.
            if on_success is not None:
                await on_success()
            return FirstPlanSaveResult(goals=0, goal_nodes=0, action_items=0, scheduled_blocks=0)

        # 1.5) 교체(supersede) — 같은 날짜의 이전 AI 계획 산출물(미시작 카드+블록)을 soft
        #      정리하고 이 계획으로 대체. 재생성→재승인 반복 시 같은 날짜에 카드/블록이
        #      겹겹이 누적되던 문제를 막는다. 빈 계획(heaviest 없음)은 위에서 이미 반환
        #      → 아무것도 지우지 않는다.
        await supersede_previous_plan(session, user_id=user_id, target_date=target_date)
        # 1.6) heaviest goal 의 기존 분해 트리 보관 — 노드도 카드/블록처럼 승인마다
        #      새로 INSERT 되므로, 보관하지 않으면 같은 트리가 무한 누적된다.
        await _archive_goal_nodes(session, goal_id=heaviest.id)

        # 2) goal_nodes — heaviest goal 트리. temp node_id → GoalNode (parent 는 relationship).
        depths = _node_depths(goal_nodes)
        for nd in goal_nodes:
            n = GoalNode()
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

        # 3.5) heaviest goal 카테고리 보정 — 'other'(인터뷰 미분류) 일 때만 액션 다수결로
        #      파생. 사용자가 이미 실카테고리를 설정했다면 덮어쓰지 않는다.
        if heaviest.category == "other":
            derived = _derive_goal_category([a.category for a in action_by_node.values()])
            if derived is not None:
                heaviest.category = derived

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

        # 5) 호출자 부수 기록(Draft 승인 마킹·온보딩 전이 등) — 같은 트랜잭션, 같은 commit.
        #    가드 트랜잭션의 commit 이 advisory lock(트랜잭션 스코프)을 해제하므로, 부수
        #    기록을 트랜잭션 밖(별도 commit)으로 빼면 그 사이가 무락 구간이 된다.
        if on_success is not None:
            await on_success()

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
    on_success: Callable[[], Awaitable[None]] | None = None,
) -> FirstPlanSaveResult:
    """승인된 Draft 를 goal 트리까지 단일 트랜잭션 영속화 + 최대 `max_retries` 회 재시도.

    정책 위반(`PolicyViolationError`)은 결정적이라 재시도하지 않고 즉시 전파한다. 그 외
    영속화 예외(IntegrityError 등)는 가드 트랜잭션이 롤백 후 재시도하고, 마지막 실패는
    원 예외를 전파한다(라우터가 `PLAN_SAVE_FAILED` 로 매핑).

    ⚠️ 가드 트랜잭션의 commit/rollback 은 트랜잭션 스코프 advisory lock 을 해제한다.
    호출자가 lock 으로 임계 구역을 보호한다면 **시도(attempt)당 lock 을 다시 잡아야**
    하므로, 재시도 루프는 라우터가 소유하고 여기엔 `max_retries=1` 을 넘기는 것을
    권장한다 (ADR-0005 §2.5.1 의 3회 재시도는 라우터 루프가 담당). `on_success` 는
    영속화와 같은 트랜잭션(같은 commit)으로 실행할 부수 기록 훅.

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
                on_success=on_success,
            )
        except PolicyViolationError:
            raise  # 결정적 — 재시도 무의미
        except Exception as exc:  # noqa: BLE001 — 롤백은 가드 트랜잭션이 보장, 재시도 후 전파
            last_exc = exc
    assert last_exc is not None
    raise last_exc
