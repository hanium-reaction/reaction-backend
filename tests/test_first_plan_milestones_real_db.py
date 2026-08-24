"""마일스톤 영속(ADR-0007 PR-2)을 **실 Postgres 위에서** 검증.

`test_plan_approve_replace.py` 의 `_persist_milestones_if_new`/`_archive_goal_nodes` 단위
테스트는 손으로 만든 `_EntitySession` 이다 — WHERE 절을 평가하지 않고(주석에 명시) 파이썬
쪽 술어로만 판정하므로, "이미 활성 마일스톤이 있으면 새로 안 만든다"는 실제로는 SQL
WHERE(`archived_at IS NULL` 등)가 옳아야 성립하는 판정인데 fake session 으로는 그 SQL 자체가
한 번도 실행되지 않는다.

ADR-0007 이 "이 설계의 관문이자 유일한 고위험 지점"이라 부른 게 바로 이 경로다 —
`_archive_goal_nodes`(모든 사용자의 모든 계획 승인이 지나가는 함수)가 필터 하나만
잘못돼도 만다라와 무관한 일반 사용자의 재승인까지 깨질 수 있다. 여기서는 실 DB로
"재승인 반복 시 마일스톤은 유지·leaf 트리만 교체·중복 생성 없음"을 직접 확인한다
(같은 문서 §검증 항목).

`db_apply_first_plan`/`_apply_once` 전체는 안 부른다 — 내부에서
`policy_guarded_transaction` 이 `session.commit()` 을 호출하는데, `real_db_session`
픽스처는 그 호출을 명시적으로 금지한다(nested-savepoint 하네스는 아직 없음, 픽스처
docstring 참고). 대신 `_persist_milestones_if_new`/`_archive_goal_nodes` 는 둘 다
commit 없이 add/flush 만 하므로 직접 호출로 같은 SQL 경로를 검증할 수 있다.

DATABASE_URL 이 없으면 전부 스킵 — `test_mandala_persist_real_db.py` 와 같은 게이트.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.user import User
from reaction_backend.orchestrator.first_plan_adapter import (
    _archive_goal_nodes,
    _persist_milestones_if_new,
)
from reaction_backend.schemas.planning import MilestoneDraft
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")


async def _seed_goal(session: AsyncSession, *, title: str = "캡스톤 프로젝트") -> Goal:
    user_id = uuid.uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="마일스톤 영속 테스트"))
    goal = Goal()
    goal.id = uuid.uuid4()
    goal.user_id = user_id
    goal.title = title
    goal.category = "study"
    goal.goal_tier = "focus"
    goal.status = "active"
    session.add(goal)
    await session.flush()
    return goal


async def _plan_node_count(
    session: AsyncSession, *, goal_id: uuid.UUID, node_type: str | None = None
) -> int:
    stmt = (
        select(func.count())
        .select_from(GoalNode)
        .where(
            GoalNode.goal_id == goal_id,
            GoalNode.tree_kind == "plan",
            GoalNode.archived_at.is_(None),
        )
    )
    if node_type is not None:
        stmt = stmt.where(GoalNode.node_type == node_type)
    return (await session.scalar(stmt)) or 0


async def test_persist_milestones_writes_real_rows(real_db_session: AsyncSession) -> None:
    goal = await _seed_goal(real_db_session)

    rows = await _persist_milestones_if_new(
        real_db_session,
        goal_id=goal.id,
        milestones=[
            MilestoneDraft(title="기초 문법", summary="변수·조건문"),
            MilestoneDraft(title="자료구조", summary=""),
            MilestoneDraft(title="배포까지", summary="CI/CD"),
        ],
    )

    assert len(rows) == 3
    stored = (
        (
            await real_db_session.execute(
                select(GoalNode)
                .where(GoalNode.goal_id == goal.id, GoalNode.node_type == "milestone")
                .order_by(GoalNode.order_index)
            )
        )
        .scalars()
        .all()
    )
    assert [n.title for n in stored] == ["기초 문법", "자료구조", "배포까지"]
    assert all(n.depth == 1 and n.parent_node_id is None for n in stored)
    assert all(n.tree_kind == "plan" and n.archived_at is None for n in stored)
    assert stored[0].why_text == "변수·조건문"


async def test_persist_milestones_is_idempotent_against_real_where_clause(
    real_db_session: AsyncSession,
) -> None:
    """이미 활성 마일스톤이 있으면(재승인) 실 SQL WHERE 로도 감지해 새로 안 만든다.

    fake session 판(test_plan_approve_replace.py)은 WHERE 를 평가하지 않아 파이썬 쪽
    이중 필터에 기대는데, 여기서는 그 SQL 자체가 옳은지를 확인한다.
    """
    goal = await _seed_goal(real_db_session)
    first = await _persist_milestones_if_new(
        real_db_session,
        goal_id=goal.id,
        milestones=[MilestoneDraft(title="기초 문법", summary="")],
    )
    assert len(first) == 1

    second = await _persist_milestones_if_new(
        real_db_session,
        goal_id=goal.id,
        milestones=[
            MilestoneDraft(title="기초 문법", summary=""),
            MilestoneDraft(title="자료구조", summary=""),
        ],
    )

    assert second == []  # 중복 생성 없음
    assert await _plan_node_count(real_db_session, goal_id=goal.id, node_type="milestone") == 1


async def test_archive_goal_nodes_spares_real_milestone_rows(real_db_session: AsyncSession) -> None:
    goal = await _seed_goal(real_db_session)
    await _persist_milestones_if_new(
        real_db_session, goal_id=goal.id, milestones=[MilestoneDraft(title="기초 문법", summary="")]
    )
    ephemeral = GoalNode()
    ephemeral.goal_id = goal.id
    ephemeral.title = "이번 4주 트리"
    ephemeral.node_type = "core"
    ephemeral.depth = 0
    ephemeral.order_index = 0
    ephemeral.is_leaf = False
    ephemeral.tree_kind = "plan"
    real_db_session.add(ephemeral)
    await real_db_session.flush()

    archived = await _archive_goal_nodes(real_db_session, goal_id=goal.id)

    assert archived == 1
    await real_db_session.refresh(ephemeral)
    assert ephemeral.archived_at is not None
    assert await _plan_node_count(real_db_session, goal_id=goal.id, node_type="milestone") == 1


async def test_reapproval_cycle_keeps_milestones_and_replaces_leaf_tree_only(
    real_db_session: AsyncSession,
) -> None:
    """전체 재승인 주기를 실 DB로 재현 — 마일스톤은 두 번째 주기까지 그대로, leaf 트리만 교체.

    ADR-0007 §검증: "재승인 반복 시 마일스톤은 유지 · leaf 만 교체 · 트리 누적 없음".
    """
    goal = await _seed_goal(real_db_session)

    # ── 1주기 승인: 마일스톤 확정 + 이번 4주 트리 ──
    milestones = [
        MilestoneDraft(title="기초 문법", summary=""),
        MilestoneDraft(title="배포까지", summary=""),
    ]
    await _persist_milestones_if_new(real_db_session, goal_id=goal.id, milestones=milestones)
    cycle1_leaf = GoalNode()
    cycle1_leaf.goal_id = goal.id
    cycle1_leaf.title = "1주기 leaf"
    cycle1_leaf.node_type = "leaf"
    cycle1_leaf.depth = 2
    cycle1_leaf.order_index = 0
    cycle1_leaf.is_leaf = True
    cycle1_leaf.tree_kind = "plan"
    real_db_session.add(cycle1_leaf)
    await real_db_session.flush()

    assert await _plan_node_count(real_db_session, goal_id=goal.id) == 3  # 마일스톤 2 + leaf 1

    # ── 2주기 재승인: _archive_goal_nodes 로 1주기 leaf 를 보관하고, 새 leaf 를 추가 ──
    archived = await _archive_goal_nodes(real_db_session, goal_id=goal.id)
    assert archived == 1  # 마일스톤은 안 셈
    reused = await _persist_milestones_if_new(
        real_db_session, goal_id=goal.id, milestones=milestones
    )
    assert reused == []  # 이미 있으니 재생성 안 함
    cycle2_leaf = GoalNode()
    cycle2_leaf.goal_id = goal.id
    cycle2_leaf.title = "2주기 leaf"
    cycle2_leaf.node_type = "leaf"
    cycle2_leaf.depth = 2
    cycle2_leaf.order_index = 0
    cycle2_leaf.is_leaf = True
    cycle2_leaf.tree_kind = "plan"
    real_db_session.add(cycle2_leaf)
    await real_db_session.flush()

    # 활성 상태: 마일스톤 2(그대로) + 2주기 leaf 1 = 3. 1주기 leaf 는 보관돼 안 잡힌다.
    assert await _plan_node_count(real_db_session, goal_id=goal.id) == 3
    assert await _plan_node_count(real_db_session, goal_id=goal.id, node_type="milestone") == 2
    active_leaves = (
        (
            await real_db_session.execute(
                select(GoalNode.title).where(
                    GoalNode.goal_id == goal.id,
                    GoalNode.node_type == "leaf",
                    GoalNode.archived_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert active_leaves == ["2주기 leaf"]  # 트리 누적 없음 — 딱 이번 주기 것만 활성
