"""PolicySnapshot repo — **실 Postgres** 로 SQL 을 고정한다 (#168).

이슈가 명시적으로 요구한 항목이다:

> ⚠️ **실 repo DB 통합 테스트 필수**: 현재 `get_active` 의 실 SQL(`is_active` 필터 +
> `version desc` 정렬)은 **테스트로 전혀 보증되지 않는다** — 실 SQL 을 `NotImplementedError`
> 로 통째 적출해도 전 스위트 통과(`conftest.py` 가 fake 로 전면 대체).

라우트 테스트(`test_policy_snapshot.py`)는 `FakePolicySnapshotRepo` 를 쓰므로 실제 WHERE·
ORDER BY·UNIQUE 제약은 여기서만 검증된다.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.policy_snapshot import PolicySnapshot
from reaction_backend.db.models.user import User
from reaction_backend.repositories.policy_snapshot_repo import PolicySnapshotRepo
from reaction_backend.schemas.common import now_kst

pytestmark = pytest.mark.usefixtures("real_db_session")


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="정책 스냅샷 테스트"))
    await session.flush()
    return user_id


async def _seed_snapshot(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    version: int,
    is_active: bool,
    daily_max_load: int = 180,
) -> PolicySnapshot:
    row = PolicySnapshot(
        user_id=user_id,
        version=version,
        is_active=is_active,
        behavioral_profile={"attention_span": 30},
        execution_constraints={"daily_max_load": daily_max_load},
        interaction_style={"recovery_tone": "normal"},
        recovery_policy={"min_recovery_step_minutes": 10},
        source="rule",
        reason_for_update=f"v{version}",
        valid_from=now_kst() - timedelta(days=30 - version),
    )
    session.add(row)
    await session.flush()
    return row


async def test_get_active_filters_on_is_active(real_db_session: AsyncSession) -> None:
    """비활성 버전이 더 높아도 활성 행만 돌려준다 — `is_active` 필터가 살아 있는지."""
    user_id = await _seed_user(real_db_session)
    await _seed_snapshot(real_db_session, user_id, version=1, is_active=True)
    await _seed_snapshot(real_db_session, user_id, version=2, is_active=False)

    active = await PolicySnapshotRepo(real_db_session).get_active(user_id)
    assert active is not None
    assert active.version == 1


async def test_get_active_prefers_the_highest_version(real_db_session: AsyncSession) -> None:
    """활성이 둘이면(이론상 없어야 하지만) 최신 버전 — `ORDER BY version DESC` 고정."""
    user_id = await _seed_user(real_db_session)
    await _seed_snapshot(real_db_session, user_id, version=1, is_active=True)
    await _seed_snapshot(real_db_session, user_id, version=3, is_active=True)

    active = await PolicySnapshotRepo(real_db_session).get_active(user_id)
    assert active is not None
    assert active.version == 3


async def test_get_active_is_scoped_to_the_user(real_db_session: AsyncSession) -> None:
    """다른 사용자의 활성 정책이 새어 나오면 안 된다."""
    mine = await _seed_user(real_db_session)
    theirs = await _seed_user(real_db_session)
    await _seed_snapshot(real_db_session, theirs, version=5, is_active=True)

    assert await PolicySnapshotRepo(real_db_session).get_active(mine) is None


async def test_create_active_closes_the_previous_row(real_db_session: AsyncSession) -> None:
    """append-only — 이전 활성 행은 삭제가 아니라 `is_active=false` + `valid_to`."""
    user_id = await _seed_user(real_db_session)
    previous = await _seed_snapshot(real_db_session, user_id, version=1, is_active=True)
    repo = PolicySnapshotRepo(real_db_session)
    now = now_kst()

    created = await repo.create_active(
        user_id,
        behavioral_profile={"attention_span": 45},
        execution_constraints={"daily_max_load": 144},
        interaction_style={"recovery_tone": "gentle"},
        recovery_policy={"min_recovery_step_minutes": 5},
        source="rule",
        reason_for_update="주간 KPI 반영",
        now=now,
    )

    assert created.version == 2
    assert created.is_active is True
    assert previous.is_active is False
    assert previous.valid_to == now
    assert len(await repo.list_history(user_id)) == 2, "이전 행이 지워졌다"
    active = await repo.get_active(user_id)
    assert active is not None and active.version == 2


async def test_next_version_uses_max_not_count(real_db_session: AsyncSession) -> None:
    """`count()+1` 이면 중간 버전이 비었을 때 기존 번호와 충돌해 UNIQUE 제약에 걸린다."""
    user_id = await _seed_user(real_db_session)
    await _seed_snapshot(real_db_session, user_id, version=7, is_active=True)

    assert await PolicySnapshotRepo(real_db_session).next_version(user_id) == 8


async def test_next_version_starts_at_one(real_db_session: AsyncSession) -> None:
    user_id = await _seed_user(real_db_session)
    assert await PolicySnapshotRepo(real_db_session).next_version(user_id) == 1


async def test_unique_constraint_blocks_duplicate_versions(real_db_session: AsyncSession) -> None:
    """`uq_policy_snapshots_user_version` 이 실제로 걸려 있는지 — 버전 계산의 안전망."""
    from sqlalchemy.exc import IntegrityError

    user_id = await _seed_user(real_db_session)
    await _seed_snapshot(real_db_session, user_id, version=1, is_active=True)

    with pytest.raises(IntegrityError):
        await _seed_snapshot(real_db_session, user_id, version=1, is_active=False)


async def test_list_history_is_newest_first(real_db_session: AsyncSession) -> None:
    user_id = await _seed_user(real_db_session)
    for v in (1, 2, 3):
        await _seed_snapshot(real_db_session, user_id, version=v, is_active=(v == 3))

    rows = await PolicySnapshotRepo(real_db_session).list_history(user_id)
    assert [r.version for r in rows] == [3, 2, 1]


async def test_rollback_shape_preserves_the_old_row(real_db_session: AsyncSession) -> None:
    """롤백은 옛 행을 되살리는 게 아니라 값을 복사한 **새 행**을 만든다 (이력 보존)."""
    user_id = await _seed_user(real_db_session)
    v1 = await _seed_snapshot(
        real_db_session, user_id, version=1, is_active=False, daily_max_load=240
    )
    await _seed_snapshot(real_db_session, user_id, version=2, is_active=True)
    repo = PolicySnapshotRepo(real_db_session)

    restored = await repo.create_active(
        user_id,
        behavioral_profile=dict(v1.behavioral_profile),
        execution_constraints=dict(v1.execution_constraints),
        interaction_style=dict(v1.interaction_style),
        recovery_policy=dict(v1.recovery_policy),
        source="user_manual",
        reason_for_update="v1 으로 롤백",
        now=now_kst(),
    )

    assert restored.version == 3
    assert restored.execution_constraints["daily_max_load"] == 240
    assert v1.version == 1 and v1.is_active is False, "옛 행은 그대로 남아야 한다"
    assert [r.version for r in await repo.list_history(user_id)] == [3, 2, 1]
