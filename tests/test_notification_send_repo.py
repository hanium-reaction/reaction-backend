"""`NotificationSendRepo` — 실 Postgres (근거 대장 §6.1 "선행 조건").

`test_notification_send_repo_sql.py` 는 `_RecordingSession` 으로 SQL **문자열**을 고정한다
(잠금 3규칙이 걸린 WHERE 절). 여기서는 그와 달리 새로 얹은 `target_action_item_id`/
`opened_at` 이 **실제로 저장·조회**되는지 — 특히 `record()` 의 명시적 `id`(서버가 발송 전에
미리 만들어 push payload 에 실어 보내는 값)가 FK 제약과 함께 실제로 동작하는지 확인한다.

DATABASE_URL 이 없으면 스킵 — 이 레포의 실 DB 테스트 공통 게이트.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.user import User
from reaction_backend.repositories.notification_send_repo import NotificationSendRepo
from reaction_backend.schemas.common import KST
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")

_SENT_AT = datetime(2026, 7, 21, 21, 0, tzinfo=KST)


async def _seed_user(session: AsyncSession) -> UUID:
    user_id = uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="알림 테스트 유저"))
    await session.flush()
    return user_id


async def _seed_action_item(session: AsyncSession, *, user_id: UUID) -> UUID:
    action_item_id = uuid4()
    session.add(
        ActionItem(
            id=action_item_id,
            user_id=user_id,
            title="알림 테스트 카드",
            target_date=_SENT_AT.date(),
        )
    )
    await session.flush()
    return action_item_id


async def test_record_persists_explicit_id_and_target_action_item(
    real_db_session: AsyncSession,
) -> None:
    """`id` 를 명시적으로 줘도(서버 default 를 안 씀) 그대로 PK 로 저장된다 — push payload
    에 실어 보낸 값과 나중에 저장된 행이 반드시 같아야 하는 계약(모듈 docstring)의 핵심.
    """
    repo = NotificationSendRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    action_item_id = await _seed_action_item(real_db_session, user_id=user_id)
    given_id = uuid4()

    row = await repo.record(
        id=given_id,
        user_id=user_id,
        notification_class="pre_card",
        sent_at=_SENT_AT,
        target_action_item_id=action_item_id,
    )

    assert row.id == given_id
    fetched = await repo.get_by_id(given_id, user_id)
    assert fetched is not None
    assert fetched.target_action_item_id == action_item_id


async def test_record_target_action_item_id_defaults_to_none(
    real_db_session: AsyncSession,
) -> None:
    repo = NotificationSendRepo(real_db_session)
    user_id = await _seed_user(real_db_session)

    row = await repo.record(
        id=uuid4(), user_id=user_id, notification_class="evening_reflection", sent_at=_SENT_AT
    )

    assert row.target_action_item_id is None


async def test_get_by_id_scopes_to_the_owning_user(real_db_session: AsyncSession) -> None:
    repo = NotificationSendRepo(real_db_session)
    owner = await _seed_user(real_db_session)
    stranger = await _seed_user(real_db_session)
    row = await repo.record(
        id=uuid4(), user_id=owner, notification_class="evening_reflection", sent_at=_SENT_AT
    )

    assert await repo.get_by_id(row.id, owner) is not None
    assert await repo.get_by_id(row.id, stranger) is None


async def test_stamp_opened_is_first_write_wins(real_db_session: AsyncSession) -> None:
    repo = NotificationSendRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    row = await repo.record(
        id=uuid4(), user_id=user_id, notification_class="evening_reflection", sent_at=_SENT_AT
    )

    first_open = _SENT_AT
    later_open = _SENT_AT.replace(hour=22)
    await repo.stamp_opened(row, first_open)
    await repo.stamp_opened(row, later_open)

    assert row.opened_at == first_open
