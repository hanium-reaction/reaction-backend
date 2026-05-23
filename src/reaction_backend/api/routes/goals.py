"""Goals — Focus/Maintain/Parked 3 tier 목표 (S26, api-contract §6).

#3-D 단계는 **mock 스텁**: DEMO_GOALS 를 tier 별 그룹으로 반환.
실제 tier 한도(Focus 3·Maintain 5) enforcement·만다라트 분해 LLM 호출은 후속(#22).
"""

from fastapi import APIRouter, status

from reaction_backend.api.mock.goals import DEMO_DECOMPOSITION, DEMO_GOALS, DemoGoal
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.goals import (
    Goal,
    GoalCreateRequest,
    GoalDecomposition,
    GoalNode,
    GoalsByTier,
    GoalUpdateRequest,
)

router = APIRouter(prefix="/goals", tags=["goals"])


def _to_schema(demo: DemoGoal) -> Goal:
    return Goal(
        goal_id=demo.goal_id,
        title=demo.title,
        category=demo.category,
        goal_tier=demo.goal_tier,
        priority_level=demo.priority_level,
        deadline=demo.deadline,
        estimated_minutes=demo.estimated_minutes,
        status=demo.status,
    )


def _find(goal_id: str) -> DemoGoal:
    """스텁은 DEMO_GOALS 의 id 만 유효 — 그 외는 404."""
    for goal in DEMO_GOALS:
        if goal.goal_id == goal_id:
            return goal
    raise ApiError(
        ErrorCode.GOAL_NOT_FOUND,
        "해당 목표를 찾을 수 없어요.",
        http_status=status.HTTP_404_NOT_FOUND,
    )


@router.get("")
async def list_goals() -> GoalsByTier:
    """[stub] 내 목표 — tier 별 그룹 (focus/maintain/parked)."""
    by_tier: dict[str, list[Goal]] = {"focus": [], "maintain": [], "parked": []}
    for demo in DEMO_GOALS:
        if demo.goal_tier in by_tier:
            by_tier[demo.goal_tier].append(_to_schema(demo))
    return GoalsByTier(
        focus=by_tier["focus"],
        maintain=by_tier["maintain"],
        parked=by_tier["parked"],
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_goal(body: GoalCreateRequest) -> Goal:
    """[stub] 신규 목표 추가. Focus 3·Maintain 5 한도 enforcement 는 후속."""
    return Goal(
        goal_id="goal_new_stub",
        title=body.title,
        category=body.category,
        goal_tier=body.goal_tier,
        priority_level=body.priority_level,
        deadline=body.deadline,
        estimated_minutes=body.estimated_minutes,
        status="active",
    )


@router.patch("/{goal_id}")
async def update_goal(goal_id: str, body: GoalUpdateRequest) -> Goal:
    """[stub] 목표 부분 수정 — 제목·마감·우선순위·tier."""
    demo = _find(goal_id)
    return Goal(
        goal_id=demo.goal_id,
        title=body.title if body.title is not None else demo.title,
        category=demo.category,
        goal_tier=body.goal_tier if body.goal_tier is not None else demo.goal_tier,
        priority_level=(
            body.priority_level if body.priority_level is not None else demo.priority_level
        ),
        deadline=body.deadline if body.deadline is not None else demo.deadline,
        estimated_minutes=demo.estimated_minutes,
        status=demo.status,
    )


@router.post("/{goal_id}/decompose")
async def decompose_goal(goal_id: str) -> GoalDecomposition:
    """[stub] Goal Structuring 호출 → goal_nodes 트리. 실제 LLM 분해는 후속."""
    _find(goal_id)
    nodes = [
        GoalNode(node_id=n.node_id, parent_id=n.parent_node_id, title=n.title, depth=n.depth)
        for n in DEMO_DECOMPOSITION
    ]
    return GoalDecomposition(
        goal_id=goal_id,
        root_node_id=DEMO_DECOMPOSITION[0].node_id,
        nodes=nodes,
    )


@router.post("/{goal_id}/park")
async def park_goal(goal_id: str) -> Goal:
    """[stub] Focus → Parked 전환."""
    demo = _find(goal_id)
    return Goal(
        goal_id=demo.goal_id,
        title=demo.title,
        category=demo.category,
        goal_tier="parked",
        priority_level=demo.priority_level,
        deadline=demo.deadline,
        estimated_minutes=demo.estimated_minutes,
        status=demo.status,
    )


@router.delete("/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_goal(goal_id: str) -> None:
    """[stub] 목표 soft delete (archived_at)."""
    _find(goal_id)
    return None
