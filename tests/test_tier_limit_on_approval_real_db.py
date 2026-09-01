"""계획 승인이 Focus≤3/Maintain≤5 한도를 실제로 지키는지 실 Postgres 로 고정 (#371).

## 무엇이 문제였나

직접 생성 경로(`POST /goals`, `goals.py::_enforce_tier_limit`)는 한도를 걸지만, 목표가
실제로 만들어지는 주 경로인 **계획 승인**(`first_plan_adapter.materialize_goals` →
`_apply_once`)에는 한도 검사가 없었다. 인터뷰가 뽑은 `tentative_tier` 를 그대로 active 로
승격시켜, 승인을 반복하면 Focus 목표가 3개를 넘겨도(예: 4/3) 아무 에러 없이 통과했다.

`first_plan_adapter._park_tier_overflow_on_approval` 가 그 사이를 메운다 — 승인 자체는
막지 않고(사용자는 이미 인터뷰를 끝냈다), 한도를 넘긴 만큼만 조용히 parked 로 돌린다.

`_FakeSession.execute()` 는 어떤 쿼리든 빈 결과를 주므로(라우트 테스트의 HTTP 경계 한계,
`test_goal_completion_real_db.py` 와 같은 이유) `GoalRepo.count_by_tier` 가 실제로 기존
active 목표 개수를 세는지는 실 DB 로만 확인할 수 있다.

DATABASE_URL 이 없으면 전부 스킵.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.user import User
from reaction_backend.orchestrator import first_plan_adapter
from reaction_backend.repositories.goal_repo import GoalRepo
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="tier 한도 테스트"))
    await session.flush()
    return user_id


async def _seed_goal(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    title: str,
    goal_tier: str = "focus",
    status: str = "active",
) -> Goal:
    g = Goal()
    g.id = uuid.uuid4()
    g.user_id = user_id
    g.title = title
    g.category = "study"
    g.goal_tier = goal_tier
    g.status = status
    session.add(g)
    await session.flush()
    return g


async def test_overflow_parks_the_non_heaviest_new_goal_keeps_existing_untouched(
    real_db_session: AsyncSession,
) -> None:
    """기존 focus 2개 + 이번 승인이 새로 올리는 focus 2개(heaviest 포함) = 4 → 1개만 parked.

    기존 2개는 이번 승인과 무관하므로 손대지 않는다. heaviest 는 방금 카드까지 받은
    목표라 최후순위로 보호된다 — 후보가 heaviest 말고도 있으면 그쪽부터 내린다.
    """
    user_id = await _seed_user(real_db_session)
    existing_a = await _seed_goal(real_db_session, user_id, title="기존A")
    existing_b = await _seed_goal(real_db_session, user_id, title="기존B")
    previously_active_ids = {existing_a.id, existing_b.id}

    new_heaviest = await _seed_goal(real_db_session, user_id, title="새목표(heaviest)")
    new_other = await _seed_goal(real_db_session, user_id, title="새목표(그외)")

    demoted = await first_plan_adapter._park_tier_overflow_on_approval(
        real_db_session,
        user_id=user_id,
        goal_rows=[existing_a, existing_b, new_heaviest, new_other],
        heaviest_id=new_heaviest.id,
        previously_active_ids=previously_active_ids,
    )

    assert demoted == ["새목표(그외)"]
    assert new_other.goal_tier == "parked"
    assert new_heaviest.goal_tier == "focus", "방금 분해·배치된 목표는 후보가 더 있으면 보호된다"
    assert existing_a.goal_tier == "focus" and existing_b.goal_tier == "focus", (
        "이미 active 였던 목표는 이번 승인과 무관하므로 건드리지 않는다"
    )

    # 파이썬 객체 속성이 아니라 실제로 flush 된 DB 행을 재조회해도 3개(한도)만 focus.
    await real_db_session.flush()
    focus_count = await GoalRepo(real_db_session).count_by_tier(user_id, "focus")
    assert focus_count == 3


async def test_no_overflow_returns_empty_and_touches_nothing(
    real_db_session: AsyncSession,
) -> None:
    """기존 1개 + 새 1개 = 2 ≤ 3 → 조정 없음."""
    user_id = await _seed_user(real_db_session)
    existing = await _seed_goal(real_db_session, user_id, title="기존")
    new_goal = await _seed_goal(real_db_session, user_id, title="새목표")

    demoted = await first_plan_adapter._park_tier_overflow_on_approval(
        real_db_session,
        user_id=user_id,
        goal_rows=[existing, new_goal],
        heaviest_id=new_goal.id,
        previously_active_ids={existing.id},
    )

    assert demoted == []
    assert existing.goal_tier == "focus" and new_goal.goal_tier == "focus"


async def test_heaviest_gets_parked_when_it_is_the_only_candidate(
    real_db_session: AsyncSession,
) -> None:
    """기존 데이터가 이미 한도를 채운 상태(focus 3)에서 heaviest 하나만 새로 올라오면,
    다른 후보가 없으므로 heaviest 도 parked — 한도를 못 지키는 예외를 두지 않는다."""
    user_id = await _seed_user(real_db_session)
    existing = [await _seed_goal(real_db_session, user_id, title=f"기존{i}") for i in range(3)]
    previously_active_ids = {g.id for g in existing}
    new_heaviest = await _seed_goal(real_db_session, user_id, title="새heaviest")

    demoted = await first_plan_adapter._park_tier_overflow_on_approval(
        real_db_session,
        user_id=user_id,
        goal_rows=[*existing, new_heaviest],
        heaviest_id=new_heaviest.id,
        previously_active_ids=previously_active_ids,
    )

    assert demoted == ["새heaviest"]
    assert new_heaviest.goal_tier == "parked"
    assert all(g.goal_tier == "focus" for g in existing)


async def test_maintain_tier_uses_the_five_cap_independently_of_focus(
    real_db_session: AsyncSession,
) -> None:
    """Maintain 은 focus 와 별개로 5 개 한도 — focus 가 가득 차 있어도 maintain 판정에 안 섞인다."""
    user_id = await _seed_user(real_db_session)
    existing_focus = await _seed_goal(real_db_session, user_id, title="focus0", goal_tier="focus")
    existing_maintain = [
        await _seed_goal(real_db_session, user_id, title=f"maintain{i}", goal_tier="maintain")
        for i in range(5)
    ]
    previously_active_ids = {existing_focus.id, *(g.id for g in existing_maintain)}
    new_maintain = await _seed_goal(
        real_db_session, user_id, title="새maintain", goal_tier="maintain"
    )

    demoted = await first_plan_adapter._park_tier_overflow_on_approval(
        real_db_session,
        user_id=user_id,
        goal_rows=[existing_focus, *existing_maintain, new_maintain],
        heaviest_id=new_maintain.id,
        previously_active_ids=previously_active_ids,
    )

    assert demoted == ["새maintain"]
    assert new_maintain.goal_tier == "parked"
    assert existing_focus.goal_tier == "focus"
    assert all(g.goal_tier == "maintain" for g in existing_maintain)
