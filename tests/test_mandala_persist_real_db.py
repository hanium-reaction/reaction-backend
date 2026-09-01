"""만다라 승인 영속화를 **실 Postgres 스키마 위에서** 검증 (마이그레이션 `1ee508b967ba`).

기존 만다라 테스트(`test_mandala_adapter.py::test_persist_mandala_*`)는 전부 fake session
이다 — `session.add()` 를 리스트에 모을 뿐이라 **DB 제약이 한 번도 실행되지 않는다**.
마이그레이션 B 가 건 제약은 6개인데(CHECK 4 + 부분 유니크 2) 그중 어느 것도 지금까지
실제로 돌아본 적이 없다. 즉 "승인이 73행을 쓴다"는 것은 검증돼 있지만 "그 73행이 DB 가
받아주는 형상인가"는 미검증이었다.

특히 위험한 것이 **재승인**이다. `persist_mandala` 는 `_archive_previous_mandala`(기존
트리에 `archived_at` 을 찍는 ORM UPDATE)와 새 root `session.add()` 를 **flush 한 번에**
묶는다(`mandala_adapter.py:282,338`). SQLAlchemy 의 unit of work 는 한 flush 안에서
INSERT 를 UPDATE 보다 먼저 낼 수 있고, 그러면 `uq_goal_nodes_mandala_root`(goal 당 활성
root 1개)가 새 root 를 막는다. fake session 에는 인덱스가 없으니 초록이고, 실 DB 에서만
빨갛다 — 사용자가 만다라를 두 번 승인하는 순간 500 이 나는 경로다.

가드는 **위반 입력을 만들어야** 검증된다. 정상 데이터만 넣는 테스트는 CHECK 를 지워도
초록이므로, 아래 3·4번은 제약을 실제로 위반하는 행을 넣어 `IntegrityError` 를 요구한다.
5번은 반대 방향 — 계획 트리(`tree_kind='plan'`)가 만다라 제약에 잘못 걸리지 않는지 본다
(가드에 `tree_kind <> 'mandala' OR ...` 를 빠뜨리면 기존 계획 분해 트리가 전부 죽는다).

DATABASE_URL 이 없으면 전부 스킵 — `tests/test_recovery_evidence_sql.py` 와 같은 게이트.
CI 의 `lint-test` 잡에는 postgres 서비스가 있어 항상 실행된다.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.habit import Habit
from reaction_backend.db.models.user import User
from reaction_backend.orchestrator import mandala_adapter
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.mandala import MandalaCell, MandalaSubgoal
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")


# ── 시드 헬퍼 — id 는 client-side 로 미리 정해 관계를 손으로 잇는다 ──


async def _seed_ultimate_goal(session: AsyncSession) -> Goal:
    user_id = uuid.uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="만다라 영속 테스트"))
    goal = Goal()
    goal.id = uuid.uuid4()
    goal.user_id = user_id
    goal.title = "메이저리그 투수"
    goal.category = "other"
    goal.goal_tier = "parked"
    goal.status = "active"
    goal.is_ultimate = True
    session.add(goal)
    await session.flush()
    return goal


def _subgoals() -> list[MandalaSubgoal]:
    """축 8개 — 제목은 §7.7 상한(10자) 안."""
    return [
        MandalaSubgoal(order_index=i, title=f"축{i}", why_text=f"축 {i} 이유", source="llm")
        for i in range(8)
    ]


def _cells() -> list[MandalaCell]:
    """축당 8칸 × 8축 = 64칸 — 제목은 §7.7 상한(16자) 안."""
    return [
        MandalaCell(subgoal_index=s, order_index=o, title=f"셀{s}-{o}", source="llm")
        for s in range(8)
        for o in range(8)
    ]


def _mandala_node(goal: Goal, **overrides: Any) -> GoalNode:
    """만다라 노드 1행 — 기본값은 제약을 만족하는 leaf. overrides 로 위반시킨다."""
    n = GoalNode()
    n.id = uuid.uuid4()
    n.goal_id = goal.id
    n.title = "셀"
    n.node_type = "leaf"
    n.depth = 2
    n.order_index = 0
    n.is_leaf = True
    n.tree_kind = "mandala"
    n.source = "llm"
    for k, v in overrides.items():
        setattr(n, k, v)
    return n


async def _active_mandala_count(session: AsyncSession, goal_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(GoalNode)
            .where(
                GoalNode.goal_id == goal_id,
                GoalNode.tree_kind == "mandala",
                GoalNode.archived_at.is_(None),
            )
        )
    ) or 0


# ── 1) 정상 경로 — 73행이 실 제약 6개를 전부 통과한다 ──


async def test_persist_mandala_writes_73_rows_under_real_constraints(
    real_db_session: AsyncSession,
) -> None:
    """승인이 실 스키마에 1 + 8 + 64 = 73행을 쓴다.

    fake session 판(`test_mandala_adapter.py`)이 세는 것과 같은 수지만, 여기서는 CHECK 4개
    (`ck_goal_nodes_tree_kind`·`source`·`mandala_shape`·`mandala_type`)와 부분 유니크 2개가
    실제로 실행된 뒤의 수다.
    """
    goal = await _seed_ultimate_goal(real_db_session)

    root, activated, _ = await mandala_adapter.persist_mandala(
        real_db_session,
        goal=goal,
        center_why_text="중앙 이유",
        subgoals=_subgoals(),
        cells=_cells(),
    )

    assert activated == 73, f"73행이어야 한다: {activated}"
    assert await _active_mandala_count(real_db_session, goal.id) == 73

    depths = (
        await real_db_session.execute(
            select(GoalNode.depth, func.count())
            .where(GoalNode.goal_id == goal.id, GoalNode.tree_kind == "mandala")
            .group_by(GoalNode.depth)
        )
    ).all()
    assert dict(depths) == {0: 1, 1: 8, 2: 64}, f"깊이별 분포가 1/8/64 가 아니다: {depths}"
    assert root.depth == 0 and root.node_type == "core"


# ── 2) 재승인 — 이 설계에서 실 DB 로만 잡히는 경로 ──


async def test_reapprove_archives_previous_tree_without_violating_root_unique(
    real_db_session: AsyncSession,
) -> None:
    """같은 goal 에 두 번 승인해도 `uq_goal_nodes_mandala_root` 에 걸리지 않는다.

    걸린다면 `persist_mandala` 가 archive UPDATE 를 새 root INSERT 보다 먼저 flush 하지
    않는다는 뜻이다(부분 유니크는 `archived_at IS NULL` 인 root 를 goal 당 1개로 묶는다).
    fake session 에는 인덱스가 없어 이 실패를 **원리적으로** 재현할 수 없다.
    """
    goal = await _seed_ultimate_goal(real_db_session)

    await mandala_adapter.persist_mandala(
        real_db_session,
        goal=goal,
        center_why_text="1차",
        subgoals=_subgoals(),
        cells=_cells(),
    )
    assert await _active_mandala_count(real_db_session, goal.id) == 73

    _, activated, _ = await mandala_adapter.persist_mandala(
        real_db_session,
        goal=goal,
        center_why_text="2차",
        subgoals=_subgoals(),
        cells=_cells(),
    )

    assert activated == 73
    assert await _active_mandala_count(real_db_session, goal.id) == 73, (
        "재승인 후 활성 만다라는 다시 73행이어야 한다(옛 트리는 archived_at 으로 빠진다)"
    )
    total = await real_db_session.scalar(
        select(func.count())
        .select_from(GoalNode)
        .where(GoalNode.goal_id == goal.id, GoalNode.tree_kind == "mandala")
    )
    assert total == 146, f"옛 트리는 하드 삭제가 아니라 보관돼 남아야 한다: {total}"


# ── 3~4) 위반 입력 — 가드가 실제로 막는지 ──


async def test_duplicate_slot_violates_partial_unique_index(
    real_db_session: AsyncSession,
) -> None:
    """같은 부모 아래 같은 `order_index` 두 칸은 DB 가 거절한다(`uq_goal_nodes_mandala_slot`).

    이 인덱스가 없으면 재생성 경로의 버그가 한 축에 9칸을 남겨도 조용히 통과한다.
    """
    goal = await _seed_ultimate_goal(real_db_session)
    root, _, _ = await mandala_adapter.persist_mandala(
        real_db_session,
        goal=goal,
        center_why_text=None,
        subgoals=_subgoals(),
        cells=[],
    )
    axis = (
        await real_db_session.execute(
            select(GoalNode).where(GoalNode.parent_node_id == root.id, GoalNode.order_index == 0)
        )
    ).scalar_one()

    real_db_session.add(_mandala_node(goal, parent_node_id=axis.id, order_index=3))
    await real_db_session.flush()

    with pytest.raises(IntegrityError):
        async with real_db_session.begin_nested():
            real_db_session.add(_mandala_node(goal, parent_node_id=axis.id, order_index=3))
            await real_db_session.flush()


async def test_mandala_type_check_rejects_depth_node_type_mismatch(
    real_db_session: AsyncSession,
) -> None:
    """깊이와 노드 타입이 어긋난 행은 `ck_goal_nodes_mandala_type` 이 막는다.

    depth=2 는 `node_type='leaf'`·`is_leaf=true` 여야 한다 — 여기서는 subgoal 로 위반시킨다.
    이 가드가 없으면 9×9 좌표 전개(§7.3)가 깨진 트리를 FE 가 렌더하려다 죽는다.
    """
    goal = await _seed_ultimate_goal(real_db_session)
    root, _, _ = await mandala_adapter.persist_mandala(
        real_db_session,
        goal=goal,
        center_why_text=None,
        subgoals=_subgoals(),
        cells=[],
    )

    with pytest.raises(IntegrityError):
        async with real_db_session.begin_nested():
            real_db_session.add(
                _mandala_node(
                    goal,
                    parent_node_id=root.id,
                    depth=2,
                    node_type="subgoal",
                    is_leaf=False,
                    order_index=0,
                )
            )
            await real_db_session.flush()


# ── 5) 반대 방향 — 가드가 계획 트리를 잘못 막지 않는다 ──


async def test_plan_tree_rows_are_exempt_from_mandala_shape_check(
    real_db_session: AsyncSession,
) -> None:
    """`tree_kind='plan'` 행은 만다라 형상 제약 밖이다.

    마이그레이션 B 의 CHECK 2개는 전부 `tree_kind <> 'mandala' OR ...` 가드로 시작한다
    (§3.5) — 기존 계획 분해 트리는 depth·order_index 에 만다라 같은 상한이 없어서, 가드를
    빠뜨리면 배포 즉시 기존 행이 제약을 위반한다. 만다라라면 거절될 형상(depth=5,
    order_index=42, subgoal 인데 leaf)을 plan 으로 넣어 통과를 확인한다.
    """
    goal = await _seed_ultimate_goal(real_db_session)

    node = _mandala_node(
        goal,
        tree_kind="plan",
        depth=5,
        order_index=42,
        node_type="subgoal",
        is_leaf=True,
    )
    real_db_session.add(node)
    await real_db_session.flush()

    stored = await real_db_session.get(GoalNode, node.id)
    assert stored is not None and stored.tree_kind == "plan"


# ── 6) fetch_promoted_goal_titles_for_user — 계획 인터뷰 goals.heaviest 입력(ADR-0008 §8 "B") ──


async def _promoted_goal(session: AsyncSession, *, ultimate: Goal, status: str) -> Goal:
    """만다라 축에서 승격된 것처럼 Goal 을 시드 — `GoalNode.promoted_goal_id` 로 잇는다.

    axis 노드의 `goal_id` 는 이 만다라 트리의 주인(ultimate)을, `promoted_goal_id` 는 새로
    만든 학기 목표를 가리킨다 — `promote_mandala_node`(U10)와 같은 관계 방향.
    """
    promoted = Goal()
    promoted.id = uuid.uuid4()
    promoted.user_id = ultimate.user_id
    promoted.title = "메이저리그 드래프트 1순위"
    promoted.category = "other"
    promoted.goal_tier = "focus"
    promoted.status = status
    promoted.is_ultimate = False
    session.add(promoted)
    await session.flush()
    axis = _mandala_node(ultimate, depth=1, node_type="subgoal", title="구위", is_leaf=False)
    axis.promoted_goal_id = promoted.id
    session.add(axis)
    await session.flush()
    return promoted


async def test_fetch_promoted_goal_titles_includes_proposed_status(
    real_db_session: AsyncSession,
) -> None:
    """승격 직후엔 항상 'proposed'(U10) — 이 상태부터 보여야 갓 승격한 축이 안 사라진다."""
    ultimate = await _seed_ultimate_goal(real_db_session)
    promoted = await _promoted_goal(real_db_session, ultimate=ultimate, status="proposed")

    titles = await mandala_adapter.fetch_promoted_goal_titles_for_user(
        real_db_session, ultimate.user_id
    )

    assert titles == [promoted.title]


async def test_fetch_promoted_goal_titles_includes_active_status(
    real_db_session: AsyncSession,
) -> None:
    ultimate = await _seed_ultimate_goal(real_db_session)
    promoted = await _promoted_goal(real_db_session, ultimate=ultimate, status="active")

    titles = await mandala_adapter.fetch_promoted_goal_titles_for_user(
        real_db_session, ultimate.user_id
    )

    assert titles == [promoted.title]


async def test_fetch_promoted_goal_titles_excludes_archived_goal(
    real_db_session: AsyncSession,
) -> None:
    ultimate = await _seed_ultimate_goal(real_db_session)
    goal = await _promoted_goal(real_db_session, ultimate=ultimate, status="proposed")
    goal.archived_at = goal.created_at  # 아무 non-null 값 — soft delete 흉내
    await real_db_session.flush()

    titles = await mandala_adapter.fetch_promoted_goal_titles_for_user(
        real_db_session, ultimate.user_id
    )

    assert titles == []


async def test_fetch_promoted_goal_titles_scoped_to_user(real_db_session: AsyncSession) -> None:
    """다른 사용자가 승격한 목표는 절대 안 섞인다."""
    mine = await _seed_ultimate_goal(real_db_session)
    other = await _seed_ultimate_goal(real_db_session)
    await _promoted_goal(real_db_session, ultimate=other, status="proposed")

    titles = await mandala_adapter.fetch_promoted_goal_titles_for_user(
        real_db_session, mine.user_id
    )

    assert titles == []


async def test_fetch_promoted_goal_titles_excludes_non_promoted_goal(
    real_db_session: AsyncSession,
) -> None:
    """일반(승격 아닌) Goal 은 goal_nodes 와 아예 연결이 없으니 후보에 안 낀다."""
    ultimate = await _seed_ultimate_goal(real_db_session)
    plain = Goal()
    plain.id = uuid.uuid4()
    plain.user_id = ultimate.user_id
    plain.title = "그냥 목표"
    plain.category = "other"
    plain.goal_tier = "maintain"
    plain.status = "active"
    plain.is_ultimate = False
    real_db_session.add(plain)
    await real_db_session.flush()

    titles = await mandala_adapter.fetch_promoted_goal_titles_for_user(
        real_db_session, ultimate.user_id
    )

    assert titles == []


# ── 7) fetch_promoted_active_goals_for_user — 다음 2주 제안 대상(ADR-0008 §8 "G") ──


async def test_fetch_promoted_active_goals_includes_active_status(
    real_db_session: AsyncSession,
) -> None:
    ultimate = await _seed_ultimate_goal(real_db_session)
    promoted = await _promoted_goal(real_db_session, ultimate=ultimate, status="active")

    goals = await mandala_adapter.fetch_promoted_active_goals_for_user(
        real_db_session, ultimate.user_id
    )

    assert [g.id for g in goals] == [promoted.id]


async def test_fetch_promoted_active_goals_excludes_proposed_status(
    real_db_session: AsyncSession,
) -> None:
    """`fetch_promoted_goal_titles_for_user` 와 달리 'proposed' 는 대상이 아니다 — 아직
    `/plans/generate` 조차 안 거쳐 action_item 이 없으니 판정 자체가 무의미하다."""
    ultimate = await _seed_ultimate_goal(real_db_session)
    await _promoted_goal(real_db_session, ultimate=ultimate, status="proposed")

    goals = await mandala_adapter.fetch_promoted_active_goals_for_user(
        real_db_session, ultimate.user_id
    )

    assert goals == []


async def test_fetch_promoted_active_goals_excludes_archived_goal(
    real_db_session: AsyncSession,
) -> None:
    ultimate = await _seed_ultimate_goal(real_db_session)
    goal = await _promoted_goal(real_db_session, ultimate=ultimate, status="active")
    goal.archived_at = goal.created_at
    await real_db_session.flush()

    goals = await mandala_adapter.fetch_promoted_active_goals_for_user(
        real_db_session, ultimate.user_id
    )

    assert goals == []


async def test_fetch_promoted_active_goals_scoped_to_user(real_db_session: AsyncSession) -> None:
    mine = await _seed_ultimate_goal(real_db_session)
    other = await _seed_ultimate_goal(real_db_session)
    await _promoted_goal(real_db_session, ultimate=other, status="active")

    goals = await mandala_adapter.fetch_promoted_active_goals_for_user(
        real_db_session, mine.user_id
    )

    assert goals == []


# ── 7) 다시 세우기 승계 — FK 순서와 습관 부분 유니크가 걸리는 유일한 자리 ──


async def _linked_habit(session: AsyncSession, *, user_id: uuid.UUID, node: GoalNode) -> Habit:
    """만다라 칸에 링크된 반복형 습관(U12) 1행 — `uq_habits_goal_node_id_active` 대상."""
    h = Habit()
    h.id = uuid.uuid4()
    h.user_id = user_id
    h.title = "코딩테스트 1일 1문제"
    h.category = "other"
    h.frequency_per_week = 5
    h.minutes_per_session = 30
    h.time_preference = "anytime"
    h.priority_level = 3
    h.goal_node_id = node.id
    session.add(h)
    await session.flush()
    return h


async def _active_cell(session: AsyncSession, goal: Goal, title: str) -> GoalNode:
    return (
        await session.execute(
            select(GoalNode).where(
                GoalNode.goal_id == goal.id,
                GoalNode.depth == 2,
                GoalNode.title == title,
                GoalNode.archived_at.is_(None),
            )
        )
    ).scalar_one()


async def test_rebuild_repoints_habit_link_to_the_new_cell(
    real_db_session: AsyncSession,
) -> None:
    """제목이 그대로인 칸이면 습관 링크가 새 leaf 로 옮겨 붙는다 — 실 FK·유니크 위에서.

    이 경로는 fake session 으로는 검증할 수 없다. `habits.goal_node_id` UPDATE 가 새 leaf
    INSERT 보다 먼저 나가면 FK 위반이고, 옛 링크를 안 끊은 채 새 링크를 걸면
    `uq_habits_goal_node_id_active`(활성 습관 1개/칸)에 걸린다. 둘 다 인덱스가 있는 DB
    에서만 빨갛다.
    """
    goal = await _seed_ultimate_goal(real_db_session)
    await mandala_adapter.persist_mandala(
        real_db_session, goal=goal, center_why_text=None, subgoals=_subgoals(), cells=_cells()
    )
    old_cell = await _active_cell(real_db_session, goal, "셀0-0")
    habit = await _linked_habit(real_db_session, user_id=goal.user_id, node=old_cell)

    _, _, carried = await mandala_adapter.persist_mandala(
        real_db_session, goal=goal, center_why_text=None, subgoals=_subgoals(), cells=_cells()
    )
    await real_db_session.flush()

    new_cell = await _active_cell(real_db_session, goal, "셀0-0")
    assert new_cell.id != old_cell.id, "다시 세우면 칸은 새 행이다(옛 행은 보관)"
    assert habit.goal_node_id == new_cell.id
    assert carried.linked_habits == 1
    assert carried.dropped_linked_habits == ()


async def test_rebuild_unlinks_habit_when_its_cell_disappears(
    real_db_session: AsyncSession,
) -> None:
    """새 트리에 그 칸이 없으면 링크만 끊는다 — 습관 행은 살아 있어야 한다(하드 삭제 금지)."""
    goal = await _seed_ultimate_goal(real_db_session)
    await mandala_adapter.persist_mandala(
        real_db_session, goal=goal, center_why_text=None, subgoals=_subgoals(), cells=_cells()
    )
    old_cell = await _active_cell(real_db_session, goal, "셀0-0")
    habit = await _linked_habit(real_db_session, user_id=goal.user_id, node=old_cell)

    renamed = [
        MandalaSubgoal(order_index=i, title=f"새축{i}", why_text=None, source="llm")
        for i in range(8)
    ]
    _, _, carried = await mandala_adapter.persist_mandala(
        real_db_session, goal=goal, center_why_text=None, subgoals=renamed, cells=_cells()
    )
    await real_db_session.flush()

    assert habit.goal_node_id is None
    assert habit.archived_at is None
    assert carried.linked_habits == 0
    assert carried.dropped_linked_habits == (habit.title,)


async def test_rebuild_carries_promotion_and_completion_to_matching_titles(
    real_db_session: AsyncSession,
) -> None:
    """축 승격(`promoted_goal_id`)과 완료 표시(`completed_at`)는 같은 제목 자리로 이어진다."""
    goal = await _seed_ultimate_goal(real_db_session)
    root, _, _ = await mandala_adapter.persist_mandala(
        real_db_session, goal=goal, center_why_text=None, subgoals=_subgoals(), cells=_cells()
    )
    promoted = Goal()
    promoted.id = uuid.uuid4()
    promoted.user_id = goal.user_id
    promoted.title = "이번 학기 구위 올리기"
    promoted.category = "other"
    promoted.goal_tier = "focus"
    promoted.status = "proposed"
    promoted.is_ultimate = False
    real_db_session.add(promoted)
    await real_db_session.flush()

    axis = (
        await real_db_session.execute(
            select(GoalNode).where(GoalNode.parent_node_id == root.id, GoalNode.order_index == 0)
        )
    ).scalar_one()
    axis.promoted_goal_id = promoted.id
    cell = await _active_cell(real_db_session, goal, "셀0-0")
    cell.completed_at = now_kst()
    await real_db_session.flush()

    _, _, carried = await mandala_adapter.persist_mandala(
        real_db_session, goal=goal, center_why_text=None, subgoals=_subgoals(), cells=_cells()
    )
    await real_db_session.flush()

    new_axis = (
        await real_db_session.execute(
            select(GoalNode).where(
                GoalNode.goal_id == goal.id,
                GoalNode.depth == 1,
                GoalNode.order_index == 0,
                GoalNode.archived_at.is_(None),
            )
        )
    ).scalar_one()
    new_cell = await _active_cell(real_db_session, goal, "셀0-0")
    assert new_axis.promoted_goal_id == promoted.id
    assert new_cell.completed_at is not None
    assert (carried.promoted_axes, carried.completed_cells) == (1, 1)
    # 승격된 목표 자체는 건드리지 않는다 — 축 배지만 새 축으로 옮겨간다.
    assert promoted.archived_at is None and promoted.status == "proposed"
