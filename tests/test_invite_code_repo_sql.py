"""InviteCodeRepo + UserRepo.count_signed_up — 실 Postgres (#324).

`FakeInviteCodeRepo`/`FakeUserRepo`(conftest)는 인메모리라 유일성 제약(`code` unique)이나
실제 COUNT 쿼리를 한 번도 실행하지 않는다 — 여기서 실 DB 로 그 부분만 고정한다.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.user import User
from reaction_backend.repositories.invite_code_repo import InviteCodeRepo
from reaction_backend.repositories.user_repo import UserRepo
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")


async def test_create_rejects_duplicate_code(real_db_session: AsyncSession) -> None:
    repo = InviteCodeRepo(real_db_session)
    await repo.create("DUPLICATE-CODE")
    await real_db_session.flush()

    with pytest.raises(IntegrityError):
        async with real_db_session.begin_nested():
            await repo.create("DUPLICATE-CODE")
            await real_db_session.flush()


async def test_get_by_code_is_case_insensitive(real_db_session: AsyncSession) -> None:
    repo = InviteCodeRepo(real_db_session)
    await repo.create("reaction-reviewer", note="Play 리뷰어")
    await real_db_session.flush()

    row = await repo.get_by_code("  REACTION-Reviewer  ")
    assert row is not None
    assert row.note == "Play 리뷰어"


async def test_mark_used_persists(real_db_session: AsyncSession) -> None:
    repo = InviteCodeRepo(real_db_session)
    row = await repo.create("MARK-ME")
    user = User(email="mark-used-324@test.local", name="Marker")
    real_db_session.add(user)
    await real_db_session.flush()
    assert row.used_at is None

    await repo.mark_used(row, used_by_user_id=user.id)

    reloaded = await repo.get_by_code("MARK-ME")
    assert reloaded is not None
    assert reloaded.used_at is not None
    assert reloaded.used_by_user_id == user.id


async def test_count_signed_up_excludes_archived(real_db_session: AsyncSession) -> None:
    from datetime import UTC, datetime

    repo = UserRepo(real_db_session)
    before = await repo.count_signed_up()

    active = User(email="active-324@test.local", name="Active")
    archived = User(email="archived-324@test.local", name="Archived")
    archived.archived_at = datetime.now(UTC)
    real_db_session.add_all([active, archived])
    await real_db_session.flush()

    after = await repo.count_signed_up()
    assert after == before + 1  # active 만 +1, archived 는 안 셈
