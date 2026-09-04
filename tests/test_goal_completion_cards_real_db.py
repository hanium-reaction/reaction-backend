"""목표를 끝내면 **남은 카드도 멈추는가** — 실 Postgres (ADR-0007 6b).

아침 브리프·오늘 화면·알림은 전부 `action_items` 를 **목표 상태와 무관하게** 읽는다
(`Goal` 에 join 하는 곳은 tier 조회용뿐이다). 그래서 완료 확정이 카드를 정리하지 않으면
사용자가 "이 목표 끝났어요" 를 눌러도 다음 날 아침 그 목표 카드가 그대로 뜬다 —
제품 톤("Be on your side, not on your case")과 정반대다.

읽는 쪽을 여러 군데 고치는 대신 완료 시점에 한 번 정리한다. 그 판정은 SQL WHERE 와
`_replaceable_action` 술어에 걸려 있어 fake session 으로는 검증되지 않는다.

⚠️ `supersede_previous_plan` 은 **flush 하지 않는다**(승인 트랜잭션이 뒤에서 commit 한다).
그래서 호출 직후 `refresh()` 를 하면 아직 DB 에 안 간 변경이 되돌려져 **모든 단언이
무의미해진다** — 실제로 "보존" 테스트들이 그 상태로 통과하고 있었다. 반드시 flush 후에
확인한다.

DATABASE_URL 이 없으면 스킵 — 다른 real-DB 테스트와 같은 게이트.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.orchestrator import first_plan_adapter
from reaction_backend.schemas.common import now_kst
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="완료 카드 정리"))
    await session.flush()
    return user_id


async def _seed_goal(session: AsyncSession, user_id: uuid.UUID | None = None) -> Goal:
    """`user_id` 를 넘기면 **같은 사용자** 아래 목표를 만든다.

    안 넘기면 매번 새 사용자다 — 목표 스코프를 보려는 테스트가 그걸 쓰면 `user_id` 필터
    만으로 통과해 **`goal_id` 조건을 지워도 초록이 된다**(실제로 그랬다).
    """
    if user_id is None:
        user_id = await _seed_user(session)
    goal = Goal()
    goal.id = uuid.uuid4()
    goal.user_id = user_id
    goal.title = "웹 개발"
    goal.category = "study"
    goal.goal_tier = "focus"
    goal.status = "active"
    session.add(goal)
    await session.flush()
    return goal


async def _seed_mandala_node(session: AsyncSession, goal: Goal) -> GoalNode:
    """만다라 셀 하나 (`tree_kind='mandala'`) — 궁극목표 카드가 달리는 자리.

    승인 경로는 이 노드에 달린 카드를 **절대 교체하지 않는다**(W2, `1ee508b967ba`).
    완료 경로는 반대로 멈춰야 한다 — 그 갈림이 #367 이다.
    """
    n = GoalNode()
    n.id = uuid.uuid4()
    n.goal_id = goal.id
    n.title = "만다라 셀"
    n.node_type = "leaf"
    n.depth = 2
    n.order_index = 0
    n.is_leaf = True
    n.tree_kind = "mandala"
    n.source = "llm"
    session.add(n)
    await session.flush()
    return n


async def _seed_card(
    session: AsyncSession,
    goal: Goal,
    *,
    title: str,
    status: str = "planned",
    source: str = "goal",
    block_source: str | None = "ai_plan",
    goal_node_id: uuid.UUID | None = None,
) -> tuple[ActionItem, ScheduledBlock | None]:
    a = ActionItem()
    a.id = uuid.uuid4()
    a.user_id = goal.user_id
    a.goal_id = goal.id
    a.goal_node_id = goal_node_id
    a.title = title
    a.target_date = now_kst().date() + timedelta(days=3)
    a.category = "study"
    a.status = status
    a.source = source
    session.add(a)
    await session.flush()
    if block_source is None:
        return a, None
    b = ScheduledBlock()
    b.id = uuid.uuid4()
    b.user_id = goal.user_id
    b.action_item_id = a.id
    b.start_at = now_kst() + timedelta(days=3)
    b.end_at = b.start_at + timedelta(minutes=50)
    b.block_status = "scheduled"
    b.source = block_source
    session.add(b)
    await session.flush()
    return a, b


async def _complete_goal_cleanup(
    session: AsyncSession, *, user_id: uuid.UUID, goal_id: uuid.UUID
) -> int:
    """완료 라우트(`PATCH /goals/{goalId}/complete`)가 부르는 것과 **같은 인자**로 호출한다.

    두 축(`include_mandala`/`include_recovery`)을 이 파일에 한 번만 적어 둔다 — 테스트가
    라우트와 다른 인자로 부르면 이 파일 전체가 실제로 도는 경로를 검증하지 않게 된다(#367).
    """
    return await first_plan_adapter.supersede_previous_plan(
        session,
        user_id=user_id,
        goal_id=goal_id,
        include_mandala=True,
        include_recovery=True,
    )


async def test_completion_cancels_untouched_planned_cards(real_db_session: AsyncSession) -> None:
    """끝냈다고 확인하면 남은 예정 카드와 그 블록이 soft 정리된다."""
    goal = await _seed_goal(real_db_session)
    card, block = await _seed_card(real_db_session, goal, title="3주차 세션")

    await _complete_goal_cleanup(real_db_session, user_id=goal.user_id, goal_id=goal.id)
    await real_db_session.flush()  # 이 함수는 flush 하지 않는다 — 안 하면 refresh 가 되돌린다
    await real_db_session.refresh(card)
    assert block is not None
    await real_db_session.refresh(block)

    assert card.archived_at is not None  # soft — 행은 남는다(AGENTS §2)
    assert block.block_status == "cancelled"


async def test_completion_preserves_started_and_finished_cards(
    real_db_session: AsyncSession,
) -> None:
    """이미 시작·완료한 카드는 남는다 — 이력이고, 사용자가 실제로 한 일이다."""
    goal = await _seed_goal(real_db_session)
    started, _ = await _seed_card(real_db_session, goal, title="진행 중", status="in_progress")
    done, _ = await _seed_card(real_db_session, goal, title="완료", status="done")

    await _complete_goal_cleanup(real_db_session, user_id=goal.user_id, goal_id=goal.id)
    await real_db_session.flush()  # 이 함수는 flush 하지 않는다 — 안 하면 refresh 가 되돌린다
    for a in (started, done):
        await real_db_session.refresh(a)

    assert started.archived_at is None
    assert done.archived_at is None


async def test_completion_preserves_cards_the_user_moved(real_db_session: AsyncSession) -> None:
    """사용자가 시간을 옮긴(`user_edit`) 블록을 가진 카드는 통째로 보존된다.

    직접 옮겼다는 건 "이건 내가 하겠다" 는 의사표시다 — 목표를 닫아도 그건 남는다.
    """
    goal = await _seed_goal(real_db_session)
    moved, block = await _seed_card(
        real_db_session, goal, title="내가 옮긴 것", block_source="user_edit"
    )

    await _complete_goal_cleanup(real_db_session, user_id=goal.user_id, goal_id=goal.id)
    await real_db_session.flush()  # 이 함수는 flush 하지 않는다 — 안 하면 refresh 가 되돌린다
    await real_db_session.refresh(moved)
    assert block is not None
    await real_db_session.refresh(block)

    assert moved.archived_at is None
    assert block.block_status == "scheduled"


async def test_completion_leaves_other_goals_alone(real_db_session: AsyncSession) -> None:
    """다른 목표의 카드는 안 건드린다 — 하나를 끝냈다고 나머지가 멈추면 안 된다.

    **같은 사용자** 아래 두 목표여야 한다. 사용자가 다르면 `user_id` 필터만으로 통과해
    `goal_id` 조건을 지워도 초록이다 — 이 테스트가 이름값을 못 한다.
    """
    user_id = await _seed_user(real_db_session)
    mine = await _seed_goal(real_db_session, user_id)
    other = await _seed_goal(real_db_session, user_id)
    theirs, _ = await _seed_card(real_db_session, other, title="다른 목표의 카드")

    await _complete_goal_cleanup(real_db_session, user_id=mine.user_id, goal_id=mine.id)
    await real_db_session.flush()
    await real_db_session.refresh(theirs)

    assert theirs.archived_at is None


async def test_completion_leaves_manual_and_inbox_cards_alone(
    real_db_session: AsyncSession,
) -> None:
    """직접 만든/인박스에서 온 카드는 AI 계획 산출물이 아니다 — 정리 대상이 아니다."""
    goal = await _seed_goal(real_db_session)
    manual, _ = await _seed_card(real_db_session, goal, title="직접 추가", source="manual")

    await _complete_goal_cleanup(real_db_session, user_id=goal.user_id, goal_id=goal.id)
    await real_db_session.flush()  # 이 함수는 flush 하지 않는다 — 안 하면 refresh 가 되돌린다
    await real_db_session.refresh(manual)

    assert manual.archived_at is None


# ───────────────── #367 만다라 유래 카드 · 회복 카드 ─────────────────
#
# 이 두 묶음은 **같은 카드를 두 경로에 태워** 답이 갈리는지를 본다. 한쪽만 있으면
# "규칙을 함수에 못 박아도 초록" 이라 #367 의 재발을 못 막는다.


async def test_completion_stops_mandala_derived_cards(real_db_session: AsyncSession) -> None:
    """만다라 셀에서 온 카드도 완료하면 멈춘다 (#367).

    **궁극목표는 카드가 전부 이것이다.** 이게 안 되면 완료를 눌러도 한 장도 안 멈춰
    증상이 100% 그대로 남는다 — 실제로 그랬다.
    """
    goal = await _seed_goal(real_db_session)
    node = await _seed_mandala_node(real_db_session, goal)
    card, block = await _seed_card(
        real_db_session, goal, title="만다라 유래 카드", goal_node_id=node.id
    )

    await _complete_goal_cleanup(real_db_session, user_id=goal.user_id, goal_id=goal.id)
    await real_db_session.flush()
    await real_db_session.refresh(card)
    assert block is not None
    await real_db_session.refresh(block)

    assert card.archived_at is not None
    assert block.block_status == "cancelled"


async def test_approval_still_preserves_mandala_derived_cards(
    real_db_session: AsyncSession,
) -> None:
    """반대로 **승인 경로**(기본 인자)는 만다라 유래 카드를 그대로 둔다 (W2 회귀 가드).

    #367 은 완료 경로만 고치는 것이다. 계획 재생성이 만다라 73칸을 쓸어가면 안 된다는
    `1ee508b967ba` 의 이유는 그대로 유효하다.
    """
    goal = await _seed_goal(real_db_session)
    node = await _seed_mandala_node(real_db_session, goal)
    card, _ = await _seed_card(
        real_db_session, goal, title="만다라 유래 카드", goal_node_id=node.id
    )

    replaced = await first_plan_adapter.supersede_previous_plan(
        real_db_session, user_id=goal.user_id, goal_id=goal.id
    )
    await real_db_session.flush()
    await real_db_session.refresh(card)

    assert replaced == 0
    assert card.archived_at is None


async def test_completion_stops_recovery_cards(real_db_session: AsyncSession) -> None:
    """수락한 회복 카드도 완료하면 멈춘다 (#367).

    회복은 '그 목표를 계속하는 다른 방법' 이다 — 목표가 끝났으면 같이 끝난다.
    `goal_id` 를 물려받게 된 뒤에야(`create_from_recovery`) 목표 스코프에 걸린다.
    """
    goal = await _seed_goal(real_db_session)
    card, _ = await _seed_card(
        real_db_session, goal, title="회복 카드", source="recovery_carryover"
    )

    await _complete_goal_cleanup(real_db_session, user_id=goal.user_id, goal_id=goal.id)
    await real_db_session.flush()
    await real_db_session.refresh(card)

    assert card.archived_at is not None


async def test_approval_still_preserves_recovery_cards(real_db_session: AsyncSession) -> None:
    """반대로 **승인 경로**는 회복 카드를 남긴다 — 계획 재생성이 회복을 지우면 안 된다."""
    goal = await _seed_goal(real_db_session)
    card, _ = await _seed_card(
        real_db_session, goal, title="회복 카드", source="recovery_downscope"
    )

    replaced = await first_plan_adapter.supersede_previous_plan(
        real_db_session, user_id=goal.user_id, goal_id=goal.id
    )
    await real_db_session.flush()
    await real_db_session.refresh(card)

    assert replaced == 0
    assert card.archived_at is None
