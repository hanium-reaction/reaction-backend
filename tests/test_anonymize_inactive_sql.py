"""90일 비활성 익명화 — **실 Postgres** 로 WHERE 절을 고정한다 (#24).

`test_anonymize_inactive.py` 는 FakeUserRepo 로 룰만 본다. 그래서 실 SQL 의 두 조건
(`last_active_at < before`, `anonymized_at IS NULL`)이 뒤바뀌거나 빠져도 그쪽은 전부
초록이다 — 이 파일이 그 구멍을 막는다.

되돌릴 수 없는 마스킹을 트리거하는 쿼리라, "누가 대상으로 뽑히는가" 는 실 DB 로 고정할
가치가 있다. `test_invite_code_repo_sql.py` 와 같은 이유·같은 모양.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.user import User
from reaction_backend.repositories.user_repo import UserRepo
from reaction_backend.scheduler.anonymize_inactive import (
    INACTIVE_ANONYMIZE_TTL_DAYS,
    inactive_anonymize_before,
    run_anonymize_inactive_users,
)
from reaction_backend.schemas.common import now_kst

pytestmark = pytest.mark.usefixtures("real_db_session")


async def _seed_user(
    session: AsyncSession,
    *,
    days_ago: float,
    anonymized: bool = False,
    archived: bool = False,
    onboarding_state: str = "ACTIVE",
) -> User:
    now = now_kst()
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.local",
        name="익명화 대상 테스트",
        onboarding_state=onboarding_state,
        last_active_at=now - timedelta(days=days_ago),
    )
    if anonymized:
        user.is_anonymized = True
        user.anonymized_at = now
    if archived:
        user.archived_at = now
    session.add(user)
    await session.flush()
    return user


async def test_query_picks_only_stale_and_not_yet_anonymized(
    real_db_session: AsyncSession,
) -> None:
    stale = await _seed_user(real_db_session, days_ago=INACTIVE_ANONYMIZE_TTL_DAYS + 1)
    await _seed_user(real_db_session, days_ago=1)  # 최근 활동
    await _seed_user(
        real_db_session, days_ago=INACTIVE_ANONYMIZE_TTL_DAYS + 30, anonymized=True
    )  # 이미 익명화

    picked = await UserRepo(real_db_session).list_inactive_for_anonymization(
        before=inactive_anonymize_before(now_kst())
    )

    assert [u.id for u in picked] == [stale.id]


async def test_boundary_is_strictly_less_than(real_db_session: AsyncSession) -> None:
    """경계값을 **실 SQL 로** 고정 — `<` 이지 `<=` 가 아니다.

    같은 이름의 fake 테스트가 있지만 그건 `FakeUserRepo` 의 파이썬 비교를 볼 뿐이라,
    실 쿼리의 부등호를 `<=` 로 바꿔도 초록이다(뮤테이션으로 실증). 하루 일찍 익명화하는
    off-by-one 은 되돌릴 수 없는 마스킹이므로 여기서 따로 못 박는다.

    `now` 를 한 번만 계산해 seed 와 질의에 같은 값을 쓴다 — 각자 `now_kst()` 를 부르면
    마이크로초 차이로 경계가 흔들려 부등호를 구분하지 못한다.
    """
    now = now_kst()
    boundary = inactive_anonymize_before(now)

    exactly_at = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.local",
        name="정확히 경계",
        onboarding_state="ACTIVE",
        last_active_at=boundary,  # 정확히 90일 전
    )
    one_second_older = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.local",
        name="1초 더 오래",
        onboarding_state="ACTIVE",
        last_active_at=boundary - timedelta(seconds=1),
    )
    real_db_session.add_all([exactly_at, one_second_older])
    await real_db_session.flush()

    picked = {
        u.id
        for u in await UserRepo(real_db_session).list_inactive_for_anonymization(before=boundary)
    }

    assert one_second_older.id in picked, "경계보다 이른 사용자는 대상이어야 한다"
    assert exactly_at.id not in picked, (
        "정확히 경계인 사용자는 아직 대상이 아니다 (< 이지 <=가 아님)"
    )


async def test_query_ignores_onboarding_state(real_db_session: AsyncSession) -> None:
    """`list_active()` 와 달리 onboarding_state 를 안 본다 — 온보딩 중 이탈 계정도 대상."""
    mid_onboarding = await _seed_user(
        real_db_session,
        days_ago=INACTIVE_ANONYMIZE_TTL_DAYS + 5,
        onboarding_state="ONBOARDING_INTERVIEW",
    )

    picked = await UserRepo(real_db_session).list_inactive_for_anonymization(
        before=inactive_anonymize_before(now_kst())
    )

    assert mid_onboarding.id in {u.id for u in picked}


async def test_deleted_accounts_are_excluded_via_anonymized_at(
    real_db_session: AsyncSession,
) -> None:
    """계정 삭제(#321)는 `anonymized_at` 을 세우므로 자연히 빠진다 — 이중 처리 없음."""
    await _seed_user(
        real_db_session,
        days_ago=INACTIVE_ANONYMIZE_TTL_DAYS + 100,
        anonymized=True,
        archived=True,
    )

    picked = await UserRepo(real_db_session).list_inactive_for_anonymization(
        before=inactive_anonymize_before(now_kst())
    )

    assert picked == []


async def test_end_to_end_run_masks_and_is_idempotent(real_db_session: AsyncSession) -> None:
    """실 DB 에서 job 을 두 번 돌려도 두 번째는 대상 0건 (멱등)."""
    user = await _seed_user(real_db_session, days_ago=INACTIVE_ANONYMIZE_TTL_DAYS + 2)
    original_email = user.email

    first = await run_anonymize_inactive_users(real_db_session, now=now_kst())
    assert first.anonymized == 1
    assert user.is_anonymized is True
    assert user.anonymized_at is not None
    assert user.name == "[anonymized]"
    assert user.email == original_email  # 로그인 키는 보존

    second = await run_anonymize_inactive_users(real_db_session, now=now_kst())
    assert second.total == 0
    assert second.anonymized == 0
