"""알림 설정 repo — **실 Postgres** 로 경쟁·기기 공유 규칙을 고정한다 (sched-15 / sched-4).

fake repo 는 dict 라 UNIQUE 위반도, JSONB endpoint 비교도 일어나지 않는다.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.notification_setting import NotificationSetting
from reaction_backend.db.models.user import User
from reaction_backend.repositories.notification_repo import NotificationRepo
from reaction_backend.schemas.common import now_kst

pytestmark = pytest.mark.usefixtures("real_db_session")

_E = "https://fcm.googleapis.com/fcm/send/shared-device"


def _sub(endpoint: str) -> dict[str, Any]:
    return {"endpoint": endpoint, "keys": {"p256dh": "k", "auth": "a"}}


async def _user(session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.local",
        name="알림 테스트",
        last_active_at=now_kst(),
    )
    session.add(user)
    await session.flush()
    return user


async def _stored(session: AsyncSession, user_id: uuid.UUID) -> Any:
    stmt = select(NotificationSetting.push_subscription).where(
        NotificationSetting.user_id == user_id
    )
    return (await session.execute(stmt)).scalar_one()


async def test_get_or_create_survives_a_concurrent_first_insert(
    real_db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """첫 SELECT 가 '없음'을 본 사이에 다른 요청이 행을 넣은 경우 — 500 이 아니라 그 행.

    예전 코드는 add+flush 라 여기서 UNIQUE(user_id) 위반이 났다(새 사용자 첫 화면 동시 요청).
    """
    user = await _user(real_db_session)
    competitor = NotificationSetting(user_id=user.id, pre_card_enabled=True)
    real_db_session.add(competitor)
    await real_db_session.flush()
    competitor_id = competitor.id
    real_db_session.expunge(competitor)

    repo = NotificationRepo(real_db_session)
    original = repo.get_by_user
    calls = 0

    async def _first_read_misses(user_id: uuid.UUID) -> NotificationSetting | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None  # 경쟁자의 INSERT 가 커밋되기 전에 읽은 SELECT
        return await original(user_id)

    monkeypatch.setattr(repo, "get_by_user", _first_read_misses)
    setting = await repo.get_or_create(user.id)

    assert setting.id == competitor_id
    assert setting.pre_card_enabled is True  # 경쟁자의 값을 덮지 않는다


async def test_get_or_create_creates_defaults_once(real_db_session: AsyncSession) -> None:
    user = await _user(real_db_session)
    repo = NotificationRepo(real_db_session)

    first = await repo.get_or_create(user.id)
    second = await repo.get_or_create(user.id)

    assert first.id == second.id
    assert first.pre_card_enabled is False
    assert first.morning_brief_time.hour == 8


async def test_same_endpoint_moves_to_the_latest_subscriber(
    real_db_session: AsyncSession,
) -> None:
    """공용 기기 — A 가 켜고 로그아웃, B 가 같은 브라우저에서 켜면 A 의 구독은 지워진다.

    둘 다 남으면 A 의 카드 제목이 담긴 알림이 B 앞에 뜬다(sched-4).
    """
    a = await _user(real_db_session)
    b = await _user(real_db_session)
    c = await _user(real_db_session)
    repo = NotificationRepo(real_db_session)
    await repo.set_push_subscription(await repo.get_or_create(a.id), _sub(_E))
    other_device = _sub("https://fcm.googleapis.com/fcm/send/c-own-phone")
    await repo.set_push_subscription(await repo.get_or_create(c.id), other_device)

    await repo.set_push_subscription(await repo.get_or_create(b.id), _sub(_E))
    await real_db_session.flush()

    assert await _stored(real_db_session, a.id) is None
    assert await _stored(real_db_session, b.id) == _sub(_E)
    assert await _stored(real_db_session, c.id) == other_device  # 다른 기기는 그대로
