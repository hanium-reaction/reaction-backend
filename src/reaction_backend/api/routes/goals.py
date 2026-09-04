"""Goals — Focus/Maintain/Parked 3 tier 목표 (S26, api-contract §6).

Issue #22 실구현:
- CRUD 실 DB (`goals` 테이블).
- Tier 한도 enforce — Focus ≤ 3, Maintain ≤ 5 (422 `GOAL_TIER_LIMIT_EXCEEDED`, ADR-0005 §2.5.1).
- park — Focus → Parked 전환 (Parked 자유, 한도 X).
- nodes — 계획 승인 시 영속된 **실제 분해 트리** 조회. (예전 `POST /decompose` 는 목표와
  무관한 데모 트리를 돌려주던 mock stub 이라 제거했다 — `list_goal_nodes` 참고.)
- soft delete (`archived_at` + status='archived').
"""

from __future__ import annotations

from datetime import date
from http import HTTPStatus
from typing import Annotated, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.goal import GOAL_CATEGORY_VALUES
from reaction_backend.db.models.goal import Goal as GoalModel
from reaction_backend.db.models.goal_node import GoalNode as GoalNodeModel
from reaction_backend.db.models.habit import Habit as HabitModel
from reaction_backend.db.session import get_db
from reaction_backend.orchestrator import (
    first_plan_adapter,
    inbox_resources,
    mandala_adapter,
    ultimate_adapter,
)
from reaction_backend.orchestrator._common import user_agent_lock
from reaction_backend.repositories.goal_repo import GoalRepo, get_goal_repo
from reaction_backend.repositories.habit_instance_repo import (
    HabitInstanceRepo,
    get_habit_instance_repo,
)
from reaction_backend.repositories.habit_repo import (
    HabitRepo,
    current_week_start_kst,
    get_habit_repo,
)
from reaction_backend.repositories.inbox_repo import InboxRepo, get_inbox_repo
from reaction_backend.repositories.interview_repo import InterviewRepo, get_interview_repo
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.goals import (
    Goal,
    GoalCompletionRequest,
    GoalCreateRequest,
    GoalDecomposition,
    GoalNode,
    GoalNodeType,
    GoalsByTier,
    GoalTier,
    GoalUpdateRequest,
    MilestoneCompletionRequest,
)
from reaction_backend.schemas.habits import Habit as HabitSchema
from reaction_backend.schemas.mandala import (
    MandalaHabitLinkRequest,
    MandalaNode,
    MandalaNodeUpdateRequest,
    MandalaPromoteRequest,
    MandalaRebuildLinkedHabit,
    MandalaRebuildPreflightResponse,
    MandalaRebuildPromotedAxis,
    MandalaSource,
    MandalaTreeResponse,
)
from reaction_backend.schemas.ultimate_goal import UltimateGoalRequest

router = APIRouter(prefix="/goals", tags=["goals"])

_ID_PREFIX = "goal_"
_TIER_LIMITS: dict[str, int] = {"focus": 3, "maintain": 5}  # parked 자유 (DevBaseline §1.4)
_CATEGORIES = frozenset(GOAL_CATEGORY_VALUES)


def _to_schema(
    goal: GoalModel, *, promoted_from_axis: str | None = None, has_plan: bool = True
) -> Goal:
    """`has_plan` 기본값이 `True` 인 이유: 단건 응답(create/update/park)은 계획 트리를
    조회하지 않는다. 기본값을 `False` 로 두면 방금 만든 목표가 **미계획으로 잘못 칠해진다** —
    목록 새로고침 전까지. 모르는 것을 "없다" 로 단정하지 않는다."""
    return Goal(
        goal_id=f"{_ID_PREFIX}{goal.id}",
        title=goal.title,
        category=goal.category,
        goal_tier=goal.goal_tier,
        priority_level=goal.priority_level,
        deadline=goal.deadline.isoformat() if goal.deadline is not None else None,
        estimated_minutes=goal.estimated_minutes,
        status=goal.status,
        has_plan=has_plan,
        is_ultimate=goal.is_ultimate,
        promoted_from_axis=promoted_from_axis,
    )


def _parse_goal_id(goal_id: str) -> UUID:
    if not goal_id.startswith(_ID_PREFIX):
        raise _not_found()
    try:
        return UUID(goal_id[len(_ID_PREFIX) :])
    except ValueError as e:
        raise _not_found() from e


def _not_found() -> ApiError:
    return ApiError(
        ErrorCode.GOAL_NOT_FOUND,
        "해당 목표를 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _parse_deadline(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "deadline 형식이 올바르지 않아요 (YYYY-MM-DD).",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="deadline",
        ) from e


def _validate_category(category: str) -> None:
    if category not in _CATEGORIES:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            f"category 값이 올바르지 않아요 ({sorted(_CATEGORIES)} 중에서).",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="category",
        )


async def _enforce_tier_limit(repo: GoalRepo, user_id: UUID, tier: str) -> None:
    """Focus ≤ 3 / Maintain ≤ 5 한도. Parked 는 자유 (한도 X)."""
    limit = _TIER_LIMITS.get(tier)
    if limit is None:
        return
    current = await repo.count_by_tier(user_id, tier)
    if current + 1 > limit:
        raise ApiError(
            ErrorCode.GOAL_TIER_LIMIT_EXCEEDED,
            f"{tier.capitalize()} 목표는 최대 {limit}개까지 가질 수 있어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="goalTier",
        )


RepoDep = Annotated[GoalRepo, Depends(get_goal_repo)]
InboxRepoDep = Annotated[InboxRepo, Depends(get_inbox_repo)]
InterviewRepoDep = Annotated[InterviewRepo, Depends(get_interview_repo)]
HabitRepoDep = Annotated[HabitRepo, Depends(get_habit_repo)]
HabitInstanceRepoDep = Annotated[HabitInstanceRepo, Depends(get_habit_instance_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]
_ULTIMATE_LOCK_AGENT = "ultimate"


@router.get("")
async def list_goals(user: CurrentUser, repo: RepoDep, session: SessionDep) -> GoalsByTier:
    """내 목표 — tier 별 그룹 (focus / maintain / parked).

    승격된(만다라 축 유래) 목표는 카드에 그 축 제목을 실어(`promotedFromAxis`, PR7) FE 가
    "이 목표는 어느 궁극목표 축에서 왔다"는 배지를 달 수 있게 한다 — 카드마다
    `GET /goals/{id}/mandala` 를 따로 부르지 않도록 한 번에 조회.
    """
    items = await repo.list_active(user.id)
    goal_ids = [g.id for g in items]
    axis_titles = await mandala_adapter.fetch_promoted_axis_titles(session, goal_ids)
    # 계획 유무는 **한 번에** 묻는다 — 카드마다 조회하면 N+1 이다(축 배지와 같은 이유).
    planned = await repo.goal_ids_with_plan(goal_ids)
    by_tier: dict[str, list[Goal]] = {"focus": [], "maintain": [], "parked": []}
    for g in items:
        if g.goal_tier in by_tier:
            by_tier[g.goal_tier].append(
                _to_schema(
                    g,
                    promoted_from_axis=axis_titles.get(g.id),
                    has_plan=g.id in planned,
                )
            )
    return GoalsByTier(
        focus=by_tier["focus"],
        maintain=by_tier["maintain"],
        parked=by_tier["parked"],
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_goal(
    body: GoalCreateRequest,
    user: CurrentUser,
    repo: RepoDep,
    inbox_repo: InboxRepoDep,
    session: SessionDep,
) -> Goal:
    """신규 목표 — tier 한도 (Focus ≤3 / Maintain ≤5) enforce.

    생성 직후 그 카테고리의 추천 자료를 인박스에 넣는다 (#171). best-effort — savepoint
    안에서 돌기 때문에 자료 삽입이 실패해도 목표 생성은 그대로 성공한다.
    """
    _validate_category(body.category)
    await _enforce_tier_limit(repo, user.id, body.goal_tier)
    deadline = _parse_deadline(body.deadline)

    goal = await repo.create(
        user_id=user.id,
        title=body.title,
        category=body.category,
        goal_tier=body.goal_tier,
        priority_level=body.priority_level,
        deadline=deadline,
        estimated_minutes=body.estimated_minutes,
    )
    await inbox_resources.ensure_resources_best_effort(
        session, inbox_repo, user_id=user.id, goal_categories=[goal.category]
    )
    await session.commit()
    await session.refresh(goal)
    return _to_schema(goal)


@router.post("/ultimate", status_code=status.HTTP_201_CREATED)
async def upsert_ultimate_goal(
    body: UltimateGoalRequest,
    user: CurrentUser,
    interview_repo: InterviewRepoDep,
    session: SessionDep,
) -> Goal:
    """궁극목표 인터뷰(kind="ultimate") 산출물 → `Goal(status="active", tier="parked")` 확정(U1).

    이미 있으면(사용자당 1개, `Goal.is_ultimate`) **동일 행을 갱신** — 409 없이 재호출해도
    안전(재인터뷰로 궁극목표를 다듬는 정상 경로). LLM 호출 0회 — 순수 결정적 투영이라
    tier 한도(Focus≤3/Maintain≤5)도 검사하지 않는다(parked 는 애초에 한도 밖).
    """
    outcome = await ultimate_adapter.resolve_outcome(interview_repo, user.id, inline=body.outcome)
    if outcome is None:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "완료된 궁극목표 인터뷰가 없어요. 먼저 궁극목표 인터뷰를 진행해 주세요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    async with user_agent_lock(session, user.id, _ULTIMATE_LOCK_AGENT):
        goal = await ultimate_adapter.materialize_ultimate_goal(
            session, user_id=user.id, outcome=outcome
        )
        await session.commit()
        await session.refresh(goal)
    return _to_schema(goal)


@router.patch("/{goal_id}")
async def update_goal(
    goal_id: str,
    body: GoalUpdateRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> Goal:
    """목표 부분 수정. tier 변경 시 한도 재검사, category 변경 시 값 검증(#326)."""
    goal = await repo.get_by_id(user.id, _parse_goal_id(goal_id))
    if goal is None:
        raise _not_found()

    if body.goal_tier is not None and body.goal_tier != goal.goal_tier:
        await _enforce_tier_limit(repo, user.id, body.goal_tier)
    if body.category is not None:
        _validate_category(body.category)

    deadline = _parse_deadline(body.deadline) if body.deadline is not None else None
    updated = await repo.update(
        goal,
        title=body.title,
        category=body.category,
        deadline=deadline,
        priority_level=body.priority_level,
        goal_tier=body.goal_tier,
    )
    await session.commit()
    await session.refresh(updated)
    return _to_schema(updated)


@router.get("/{goal_id}/nodes")
async def list_goal_nodes(goal_id: str, user: CurrentUser, repo: RepoDep) -> GoalDecomposition:
    """이 목표의 **실제 분해 트리** — 계획 승인 시 영속된 `goal_nodes` 를 그대로 읽는다.

    이 자리에 있던 `POST /{goal_id}/decompose` 는 목표와 무관하게 하드코딩된 데모 트리
    (캡스톤 → 설계/구현/발표)를 돌려줬고 FE 가 그걸 화면에 그렸다. 어떤 목표를 분해해도
    같은 캡스톤 단계가 나왔다 — 안 보여주느니만 못한 상태라 제거하고, 실제 분해를 읽는
    조회로 대체한다. 분해 자체는 First Plan(`planning/goal_decompose` + 마일스톤)이 이미
    수행하고 승인 시 영속한다.

    계획을 아직 승인하지 않은 목표는 트리가 없으므로 `nodes=[]` (404 아님 — 목표는 존재하고
    분해만 아직 없는 정상 상태다). `rootNodeId` 도 그때는 null.
    """
    goal = await repo.get_by_id(user.id, _parse_goal_id(goal_id))
    if goal is None:
        raise _not_found()
    rows = await repo.list_nodes(goal.id)
    nodes = [
        GoalNode(
            node_id=f"node_{n.id}",
            parent_id=f"node_{n.parent_node_id}" if n.parent_node_id is not None else None,
            title=n.title,
            depth=n.depth,
            order_index=n.order_index,
            node_type=cast(GoalNodeType, n.node_type),
            is_leaf=n.is_leaf,
            completed_at=n.completed_at,
        )
        for n in rows
    ]
    root = next((n for n in rows if n.parent_node_id is None), None)
    return GoalDecomposition(
        goal_id=goal_id,
        root_node_id=f"node_{root.id}" if root is not None else None,
        nodes=nodes,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 만다라트 조회·편집·승격 (U8~U10, PR6)
# ─────────────────────────────────────────────────────────────────────────────

_NODE_PREFIX = "node_"
_HABIT_PREFIX = "habit_"  # api/routes/habits.py 의 _HABIT_PREFIX 와 반드시 같은 값


def _milestone_not_found() -> ApiError:
    """마일스톤이 아닌 노드(subgoal/leaf/만다라 칸)와 없는 id 를 **같은 코드**로 묶는다.

    구분해서 알려주면 "이 id 는 존재하나 마일스톤이 아니다" 가 새어 나간다. 사용자가
    할 수 있는 행동도 같다 — 마일스톤 목록에서 다시 고르는 것.
    """
    return ApiError(
        ErrorCode.GOAL_NOT_FOUND,
        "해당 중간 목표를 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _node_not_found() -> ApiError:
    return ApiError(
        ErrorCode.GOAL_NOT_FOUND,
        "해당 만다라 칸을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _parse_node_id(node_id: str) -> UUID:
    if not node_id.startswith(_NODE_PREFIX):
        raise _node_not_found()
    try:
        return UUID(node_id[len(_NODE_PREFIX) :])
    except ValueError as e:
        raise _node_not_found() from e


async def _load_ultimate_goal(repo: GoalRepo, user_id: UUID, goal_id: str) -> GoalModel:
    """user 소유 + `is_ultimate=True` 인 목표만 통과시킨다(`routes/planning.py` 의 만다라
    endpoint 들과 같은 가드 — 일반 목표를 만다라 대상으로 못 쓴다)."""
    goal = await repo.get_by_id(user_id, _parse_goal_id(goal_id))
    if goal is None or not goal.is_ultimate:
        raise _not_found()
    return goal


async def _load_mandala_node(repo: GoalRepo, user_id: UUID, node_id: str) -> GoalNodeModel:
    node = await repo.get_mandala_node(user_id, _parse_node_id(node_id))
    if node is None:
        raise _node_not_found()
    return node


def _to_mandala_node(
    n: GoalNodeModel,
    *,
    progress: float | None,
    coverage: float | None,
    habit_id: UUID | None,
) -> MandalaNode:
    return MandalaNode(
        node_id=f"{_NODE_PREFIX}{n.id}",
        parent_id=f"{_NODE_PREFIX}{n.parent_node_id}" if n.parent_node_id is not None else None,
        title=n.title,
        depth=n.depth,
        order_index=n.order_index,
        node_type=cast(GoalNodeType, n.node_type),
        is_leaf=n.is_leaf,
        why_text=n.why_text,
        source=cast(MandalaSource, n.source),
        locked=n.locked,
        completed_at=n.completed_at,
        promoted_goal_id=f"{_ID_PREFIX}{n.promoted_goal_id}" if n.promoted_goal_id else None,
        habit_id=f"{_HABIT_PREFIX}{habit_id}" if habit_id is not None else None,
        progress=progress,
        coverage=coverage,
    )


def _to_habit_schema(h: HabitModel) -> HabitSchema:
    return HabitSchema(
        habit_id=f"{_HABIT_PREFIX}{h.id}",
        title=h.title,
        category=h.category,
        frequency_per_week=h.frequency_per_week,
        minutes_per_session=h.minutes_per_session,
        time_preference=h.time_preference,
        priority_level=h.priority_level,
        goal_node_id=f"{_NODE_PREFIX}{h.goal_node_id}" if h.goal_node_id is not None else None,
    )


@router.get("/{goal_id}/mandala")
async def get_mandala_tree(
    goal_id: str, user: CurrentUser, repo: RepoDep, session: SessionDep
) -> MandalaTreeResponse:
    """만다라 73노드(≤) + 진척도(U8). 아직 승인된 트리가 없으면 `nodes=[]`·`rootNodeId=null`

    (404 아님 — `GET /goals/{id}/nodes` 가 미승인 계획에 이미 쓰는 것과 같은 규약).
    """
    goal = await _load_ultimate_goal(repo, user.id, goal_id)
    rows = await repo.list_nodes(goal.id, tree_kind="mandala")
    leaf_ids = [n.id for n in rows if n.depth == 2]
    actions = await mandala_adapter.fetch_actions_for_nodes(session, leaf_ids)
    habits_by_node = await mandala_adapter.fetch_habits_for_nodes(session, leaf_ids)
    instances_by_habit = await mandala_adapter.fetch_current_week_habit_instances(
        session, [h.id for h in habits_by_node.values()]
    )
    progress_map = mandala_adapter.compute_progress(
        rows, actions, habits_by_node=habits_by_node, instances_by_habit=instances_by_habit
    )

    root = next((n for n in rows if n.parent_node_id is None), None)
    nodes = []
    for n in rows:
        node_progress, node_coverage = progress_map.get(n.id, (None, None))
        linked_habit = habits_by_node.get(n.id)
        nodes.append(
            _to_mandala_node(
                n,
                progress=node_progress,
                coverage=node_coverage,
                habit_id=linked_habit.id if linked_habit is not None else None,
            )
        )
    root_progress, root_coverage = progress_map.get(root.id, (0.0, 0.0)) if root else (0.0, 0.0)
    return MandalaTreeResponse(
        goal_id=goal_id,
        root_node_id=f"{_NODE_PREFIX}{root.id}" if root is not None else None,
        statement=goal.title,
        nodes=nodes,
        progress=root_progress or 0.0,
        coverage=root_coverage or 0.0,
    )


def _rebuild_warnings(impact: mandala_adapter.MandalaRebuildImpact) -> list[str]:
    """확인 시트에 그대로 얹을 안내 문장 — 승계 규칙을 사용자 말로 옮긴 것.

    `/plans/generate` 의 `warnings` 와 같은 규약(서버가 완성된 한국어 문장을 준다)이라 FE 는
    문자열을 조립하지 않는다. 톤은 "네 편에서"(DevBaseline §1.4) — 겁주지 않고 사실만.
    """
    if not impact.has_tree:
        return []
    lines = [
        "제목이 그대로인 칸은 완료 표시와 습관 링크가 새 만다라트로 그대로 넘어가요.",
        "제목이 바뀌거나 사라진 칸은 이어지지 않아요 — 그 습관은 링크만 풀려 단독 습관으로 남아요.",
    ]
    if impact.completed_cells:
        lines.append(f"지금까지 {impact.completed_cells}칸을 완료로 표시하셨어요.")
    if impact.promoted_axes:
        titles = ", ".join(f"「{a.goal_title}」" for a in impact.promoted_axes)
        lines.append(
            f"축에서 승격한 목표 {len(impact.promoted_axes)}개({titles})는 그대로 남아요. "
            "같은 이름의 축이 새 만다라트에 없으면 축 배지만 빠져요."
        )
    if impact.live_action_items:
        lines.append(f"진행 중인 카드 {impact.live_action_items}장은 다시 세워도 사라지지 않아요.")
    return lines


@router.get("/{goal_id}/mandala/rebuild-preflight")
async def get_mandala_rebuild_preflight(
    goal_id: str, user: CurrentUser, repo: RepoDep, session: SessionDep
) -> MandalaRebuildPreflightResponse:
    """다시 세우기 사전 확인(U13) — LLM 0콜, DB 쓰기 0.

    "다시 세우기" 버튼이 확인 시트를 띄우기 위한 읽기 전용 조회다. 다시 세우면 옛 트리는
    보관되고 새 73칸이 들어서는데, 그 사이에서 사용자가 손으로 쌓은 것(완료 표시·축 승격·
    습관 링크)은 **제목이 같은 자리에만** 이어진다(`persist_mandala`). 무엇이 걸려 있는지
    승인 전에 보여주지 않으면 버튼 한 번에 조용히 끊기므로, 이 endpoint 가 HITL 의
    "무엇이 바뀌는지 먼저 보여준다" 를 만다라트 쪽에서 맡는다.

    아직 트리가 없으면 `hasTree=false` + 전부 0/빈 배열(404 아님) — 처음 세우는 경우도 FE 가
    같은 경로를 타고 확인 시트만 건너뛴다.
    """
    goal = await _load_ultimate_goal(repo, user.id, goal_id)
    rows = await repo.list_nodes(goal.id, tree_kind="mandala")
    impact = await mandala_adapter.collect_rebuild_impact(session, rows)
    return MandalaRebuildPreflightResponse(
        goal_id=goal_id,
        has_tree=impact.has_tree,
        root_node_id=f"{_NODE_PREFIX}{impact.root_node_id}" if impact.has_tree else None,
        statement=goal.title,
        total_cells=impact.total_cells,
        completed_cells=impact.completed_cells,
        promoted_axes=[
            MandalaRebuildPromotedAxis(
                order_index=a.order_index,
                axis_title=a.axis_title,
                goal_id=f"{_ID_PREFIX}{a.goal_id}",
                goal_title=a.goal_title,
                goal_status=a.goal_status,
                goal_tier=cast(GoalTier, a.goal_tier),
            )
            for a in impact.promoted_axes
        ],
        linked_habits=[
            MandalaRebuildLinkedHabit(
                subgoal_index=h.subgoal_index,
                order_index=h.order_index,
                cell_title=h.cell_title,
                habit_id=f"{_HABIT_PREFIX}{h.habit_id}",
                habit_title=h.habit_title,
                frequency_per_week=h.frequency_per_week,
            )
            for h in impact.linked_habits
        ],
        live_action_items=impact.live_action_items,
        warnings=_rebuild_warnings(impact),
    )


@router.patch("/mandala/nodes/{node_id}")
async def update_mandala_node(
    node_id: str,
    body: MandalaNodeUpdateRequest,
    user: CurrentUser,
    repo: RepoDep,
    habit_repo: HabitRepoDep,
    session: SessionDep,
) -> MandalaNode:
    """셀 상세 편집(U9) — 제목/이유/완료 토글. 준 필드만 갱신, 어떤 필드든 `source="user"` 로."""
    node = await _load_mandala_node(repo, user.id, node_id)
    touched = False
    if body.title is not None:
        limit = 10 if node.depth == 1 else 16 if node.depth == 2 else 200
        if len(body.title) > limit:
            raise ApiError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                f"제목은 최대 {limit}자예요.",
                http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
                field="title",
            )
        node.title = body.title
        touched = True
    if body.why_text is not None:
        node.why_text = body.why_text
        touched = True
    if body.completed is not None:
        node.completed_at = now_kst() if body.completed else None
        touched = True
    if touched:
        node.source = "user"
    await session.commit()
    await session.refresh(node)
    linked_habit = await habit_repo.get_active_by_goal_node(user.id, node.id)
    return _to_mandala_node(
        node,
        progress=None,
        coverage=None,
        habit_id=linked_habit.id if linked_habit is not None else None,
    )


@router.patch("/{goal_id}/nodes/{node_id}")
async def update_milestone_completion(
    goal_id: str,
    node_id: str,
    body: MilestoneCompletionRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> GoalNode:
    """마일스톤 완료 표시 — ADR-0007 §3 이 정한 **유일한 저장 예외**.

    진척도는 저장하지 않고 `action_items.status` 에서 파생한다(§3). 마일스톤 완료만
    예외인 이유는 롤업으로 표현할 수 없는 두 상태가 있어서다: "세션은 다 했는데 아직
    아니다" 와 "세션은 안 했지만 다른 경로로 달성했다". **그 판단은 AI 가 아니라 사용자
    몫**이라 자동 판정하지 않고 이 endpoint 로만 찍는다.

    `nodeType="milestone"` 인 계획 트리 노드만 받는다(저장소에서 좁힌다). subgoal/leaf 는
    404 — leaf 에 완료를 찍게 하면 `action_items.status` 와 진실이 갈린다. 만다라 칸도
    404, 그쪽은 `PATCH /goals/mandala/nodes/{nodeId}` 다.

    `completed=false` 로 되돌릴 수 있다(오조작 복구). 멱등 — 같은 값을 다시 보내도 200.
    """
    goal = await repo.get_by_id(user.id, _parse_goal_id(goal_id))
    if goal is None:
        raise _not_found()
    node = await repo.get_plan_milestone_node(user.id, goal.id, _parse_node_id(node_id))
    if node is None:
        raise _milestone_not_found()
    node.completed_at = now_kst() if body.completed else None
    # 만다라 칸과 같은 규약 — 사용자가 손댄 노드는 AI 가 채운 것과 구분된다.
    node.source = "user"
    await session.commit()
    await session.refresh(node)
    return GoalNode(
        node_id=f"{_NODE_PREFIX}{node.id}",
        parent_id=f"{_NODE_PREFIX}{node.parent_node_id}"
        if node.parent_node_id is not None
        else None,
        title=node.title,
        depth=node.depth,
        order_index=node.order_index,
        node_type=cast(GoalNodeType, node.node_type),
        is_leaf=node.is_leaf,
        completed_at=node.completed_at,
    )


@router.post("/mandala/nodes/{node_id}/promote", status_code=status.HTTP_201_CREATED)
async def promote_mandala_node(
    node_id: str,
    body: MandalaPromoteRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> Goal:
    """하위목표(축, depth=1) → 이번 학기 `Goal(status="proposed")` 승격(U10).

    **중앙(core)·셀(leaf)은 승격 대상이 아니다** — 만다라트의 "축" 단위만 학기 목표가 된다.
    이미 승격된 축을 다시 누르면(그 Goal 이 아직 살아있으면) **새로 만들지 않고 그 행을
    그대로 반환**(멱등) — U1 이 "사용자당 1개" 를 지키는 것과 같은 이유로, 같은 축을 두
    번 승격해 중복 목표가 쌓이면 안 된다.
    """
    node = await _load_mandala_node(repo, user.id, node_id)
    if node.depth != 1:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "하위목표(축)만 이번 학기 목표로 승격할 수 있어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="nodeId",
        )
    if node.promoted_goal_id is not None:
        existing = await repo.get_by_id(user.id, node.promoted_goal_id)
        if existing is not None:
            return _to_schema(existing, promoted_from_axis=node.title)

    await _enforce_tier_limit(repo, user.id, body.goal_tier)
    goal = GoalModel()
    # id 는 flush 로 받지 않고 여기서 채운다(PR5 `persist_mandala` 와 같은 이유) — 곧바로
    # `node.promoted_goal_id = goal.id` 로 써야 하고, DB 왕복(flush) 없이도 항상 값이 있어야
    # 한다(테스트의 fake session 포함).
    goal.id = uuid4()
    goal.user_id = user.id
    goal.title = node.title
    goal.category = "other"  # 만다라 축엔 category 개념이 없다 — 승격 후 PATCH 로 사용자가 조정
    goal.goal_tier = body.goal_tier
    goal.status = "proposed"
    goal.priority_level = 3
    goal.is_ultimate = False  # 승격된 goal 은 축의 파생물이지 궁극목표 자체가 아니다
    goal.why_now = node.why_text
    session.add(goal)
    await session.flush()
    node.promoted_goal_id = goal.id
    await session.commit()
    await session.refresh(goal)
    return _to_schema(goal, promoted_from_axis=node.title)


@router.post("/mandala/nodes/{node_id}/habit", status_code=status.HTTP_201_CREATED)
async def link_mandala_habit(
    node_id: str,
    body: MandalaHabitLinkRequest,
    user: CurrentUser,
    repo: RepoDep,
    habit_repo: HabitRepoDep,
    instance_repo: HabitInstanceRepoDep,
    session: SessionDep,
) -> HabitSchema:
    """셀(leaf) → 반복형 전환(U12, ADR-0008 §1). 새 `Habit` 을 만들어 이 칸에 링크한다.

    "코딩테스트 1일 1문제"·"쓰레기 줍기" 처럼 끝이 없는 칸은 계획(action_item)으로 내려보내지
    않고 이 링크로 주간 횟수(habit_instances.done_count)만 추적한다.

    **칸(leaf, depth=2)만 대상이다** — 축·중앙은 8칸을 아우르는 단위라 반복 횟수 개념이 안
    맞는다(depth≠2 면 422, `promote` 의 depth≠1 가드와 같은 자리). 이미 이 칸에 링크된 활성
    습관이 있으면 새로 만들지 않고 그 습관을 그대로 반환한다(멱등 — `promote` 와 같은 이유,
    두 번 눌러도 중복 습관이 쌓이면 안 된다).
    """
    node = await _load_mandala_node(repo, user.id, node_id)
    if node.depth != 2:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "칸(leaf)만 반복형으로 전환할 수 있어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="nodeId",
        )
    existing = await habit_repo.get_active_by_goal_node(user.id, node.id)
    if existing is not None:
        return _to_habit_schema(existing)

    habit = await habit_repo.create(
        user_id=user.id,
        title=body.title or node.title,
        category=body.category,
        frequency_per_week=body.frequency_per_week,
        minutes_per_session=body.minutes_per_session,
        time_preference=body.time_preference,
        priority_level=body.priority_level,
        goal_node_id=node.id,
    )
    # 등록 시점에 이번 주 instance 도 함께 — POST /habits 와 같은 이유(주 중간 등록이 다음
    # 월요일까지 오늘 화면에 안 보이면 안 된다).
    await instance_repo.create_or_get_for_week(
        habit_id=habit.id,
        week_start=current_week_start_kst(),
        target_count=body.frequency_per_week,
    )
    await session.commit()
    await session.refresh(habit)
    return _to_habit_schema(habit)


@router.delete("/mandala/nodes/{node_id}/habit", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_mandala_habit(
    node_id: str,
    user: CurrentUser,
    repo: RepoDep,
    habit_repo: HabitRepoDep,
    session: SessionDep,
) -> None:
    """반복형 → 프로젝트형으로 되돌리기(ADR-0008 §1) — 링크된 습관을 soft delete.

    칸(goal_node) 자체는 그대로 남는다. 링크가 없으면(이미 프로젝트형) 그냥 204 —
    "이미 그 상태"를 에러로 보지 않는다.
    """
    node = await _load_mandala_node(repo, user.id, node_id)
    habit = await habit_repo.get_active_by_goal_node(user.id, node.id)
    if habit is not None:
        await habit_repo.soft_delete(habit)
        await session.commit()
    return None


@router.post("/{goal_id}/park")
async def park_goal(goal_id: str, user: CurrentUser, repo: RepoDep, session: SessionDep) -> Goal:
    """Focus → Parked 전환. Parked 는 한도 자유."""
    goal = await repo.get_by_id(user.id, _parse_goal_id(goal_id))
    if goal is None:
        raise _not_found()
    parked = await repo.park(goal)
    await session.commit()
    await session.refresh(parked)
    return _to_schema(parked)


@router.post("/{goal_id}/complete")
async def complete_goal(
    goal_id: str,
    body: GoalCompletionRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> Goal:
    """목표 완료 확정 — "이 목표 끝난 거 맞아요?" 에 대한 사용자 확인 (ADR-0007 6b).

    마일스톤이 다 끝나면 주간 리뷰가 `goalCompletionProposals` 로 제안하지만, **여기에
    가드를 걸지 않는다.** 마일스톤 완료 표시와 같은 이유다 — 다른 경로로 달성했거나 방향이
    바뀌어 여기서 접는 경우가 있고, 그 판단은 AI 가 아니라 사용자 몫이다(§3).

    완료는 **보관(soft delete)이 아니다.** `archived_at` 을 건드리지 않아 목록에 남고,
    FE 가 `status` 로 배지를 단다. 끝낸 것과 치운 것은 다른 뜻이다.

    완료가 실제로 뜻하는 바 두 가지가 함께 성립한다:
    - **tier 한도(Focus≤3)를 더 안 먹는다** — 성실히 완주할수록 새 목표를 못 만들면 곤란하다.
    - **새 계획 후보에서 빠진다** — 안 그러면 "완료" 배지를 단 채 다음 주기 카드가 쏟아진다.

    멱등 — 같은 값을 다시 보내도 200. `completed=false` 로 되돌릴 수 있다(오조작 복구).

    ⚠️ **진행 중(`active`)인 목표만 완료할 수 있다.** `proposed` 를 완료로 보내면 해제할 때
    `active` 로 나와 계획 승인 없이 승격되고, 잠정 목표 만료 cron 대상에서도 빠진다.

    ⚠️ **되돌리기는 tier 한도를 다시 검사한다.** 완료하면 한도 집계에서 빠지므로, 안 재면
    "완료 → 새 목표 생성 → 완료 해제" 로 Focus≤3 을 넘길 수 있다(AGENTS §1 잠금 결정).
    한도가 찼으면 422 `GOAL_TIER_LIMIT_EXCEEDED` — 먼저 다른 목표를 정리해야 한다.

    완료하면 **그 목표의 남은 예정 카드와 블록이 정리된다**(soft) — 안 그러면 끝냈다고
    확인한 목표가 다음 날 아침 브리프에 그대로 뜬다. 시작·완료·실패한 카드와 사용자가
    시간을 옮긴 카드는 보존된다. **만다라 유래 카드와 회복 카드도 함께 멈춘다**(#367).
    ⚠️ **되돌려도 카드는 안 돌아온다** — 다시 하려면 되돌리기 → generate → approve 를
    거쳐야 하고, 그 되돌리기가 tier 한도에 걸릴 수 있다(soft 정리라 데이터는 남는다).
    """
    goal = await repo.get_by_id(user.id, _parse_goal_id(goal_id))
    if goal is None:
        raise _not_found()
    if body.completed:
        # 진행 중인 목표만 끝낼 수 있다. `proposed`(인터뷰가 뽑았을 뿐 계획 미승인)를
        # 완료로 보내면, 해제할 때 `active` 로 나와 **계획 승인 없이 승격**된다 —
        # HITL 게이트(`/plans/{id}/approve`)를 우회하고 14일 만료 cron 대상에서도 빠진다.
        if goal.status not in ("active", "completed"):  # 이미 완료면 멱등 no-op
            raise ApiError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                "진행 중인 목표만 완료할 수 있어요.",
                http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
                field="completed",
            )
    elif goal.status == "completed":
        # 되돌리면 다시 한도 집계 대상이 된다 — 여기서 안 재면 "완료 → 새 목표 생성 →
        # 완료 해제" 세 번으로 Focus≤3 을 넘길 수 있다(AGENTS §1 잠금 결정).
        await _enforce_tier_limit(repo, user.id, goal.goal_tier)
    updated = await repo.set_completed(goal, completed=body.completed)
    if body.completed:
        # 끝냈다고 확인했는데 남은 카드가 계속 뜨면 "이제 그만 알려줘" 가 안 지켜진다.
        # 아침 브리프·오늘 화면·알림은 전부 `action_items` 를 목표 상태와 무관하게 읽으므로,
        # 읽는 쪽을 여러 군데 고치는 대신 **여기서 한 번** 정리한다(빠뜨릴 곳이 없다).
        #
        # 승인 경로가 쓰는 것과 **같은 함수**다 — 이름은 첫 사용처(교체)에서 왔지만 하는
        # 일은 "이 목표의 손대지 않은 예정 카드와 그 블록을 soft 정리" 라 여기 그대로 맞다.
        # 시작·완료·실패한 카드와 사용자가 시간을 옮긴(user_edit) 카드는 보존된다.
        #
        # ⚠️ 두 축을 켠다 (#367). 승인 경로는 만다라 유래 카드와 회복 카드를 남겨야 하지만
        # (계획 재생성이 그것까지 쓸어가면 안 된다) **완료 경로엔 그 사정이 없다.** 안 켜면
        # 궁극목표는 카드가 전부 만다라 유래라 완료를 눌러도 한 장도 안 멈춘다.
        await first_plan_adapter.supersede_previous_plan(
            session,
            user_id=user.id,
            goal_id=goal.id,
            include_mandala=True,
            include_recovery=True,
        )
    await session.commit()
    await session.refresh(updated)
    return _to_schema(updated)


@router.delete("/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_goal(goal_id: str, user: CurrentUser, repo: RepoDep, session: SessionDep) -> None:
    """목표 soft delete (`archived_at` + `status=archived`)."""
    goal = await repo.get_by_id(user.id, _parse_goal_id(goal_id))
    if goal is None:
        raise _not_found()
    await repo.soft_delete(goal)
    await session.commit()
    return None
