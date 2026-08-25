"""`report_proximal_execution.py` 의 쿼리 조립 — 실 Postgres (근거 대장 §7.2).

판정 로직(`_had_proximal_execution`)은 `test_report_proximal_execution.py` 가 순수
함수로 이미 고정한다. 여기서는 그 판정에 넘길 재료를 **실제로 올바르게 가져오는지**만
검증한다 — 특히 `notification_class != 'pre_card'` 나 `target_action_item_id IS NULL`
인 행이 분모에 새지 않는지(fake 로는 절대 안 잡히는 WHERE 절 회귀).

DATABASE_URL 이 없으면 스킵 — 이 레포의 실 DB 테스트 공통 게이트.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from scripts.report_proximal_execution import _fetch_actual_starts, _fetch_pre_card_notifications
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.notification_send import NotificationSend
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.schemas.common import KST
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")

_SENT_AT = datetime(2026, 7, 21, 21, 0, tzinfo=KST)


async def _seed_user(session: AsyncSession) -> UUID:
    user_id = uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="근접 실행 테스트 유저"))
    await session.flush()
    return user_id


async def _seed_action_item(session: AsyncSession, *, user_id: UUID) -> UUID:
    action_item_id = uuid4()
    session.add(
        ActionItem(
            id=action_item_id,
            user_id=user_id,
            title="근접 실행 테스트 카드",
            target_date=_SENT_AT.date(),
        )
    )
    await session.flush()
    return action_item_id


async def _seed_notification(
    session: AsyncSession,
    *,
    user_id: UUID,
    notification_class: str,
    target_action_item_id: UUID | None,
    sent_at: datetime = _SENT_AT,
) -> None:
    session.add(
        NotificationSend(
            id=uuid4(),
            user_id=user_id,
            notification_class=notification_class,
            sent_at=sent_at,
            target_action_item_id=target_action_item_id,
        )
    )
    await session.flush()


async def _seed_execution(
    session: AsyncSession, *, user_id: UUID, action_item_id: UUID, actual_start_at: datetime | None
) -> None:
    block_id = uuid4()
    plan_start_at = actual_start_at or _SENT_AT
    session.add(
        ScheduledBlock(
            id=block_id,
            user_id=user_id,
            action_item_id=action_item_id,
            start_at=plan_start_at,
            end_at=plan_start_at + timedelta(minutes=30),
        )
    )
    await session.flush()
    session.add(
        ExecutionEvent(
            id=uuid4(),
            action_item_id=action_item_id,
            scheduled_block_id=block_id,
            user_id=user_id,
            plan_start_at=plan_start_at,
            plan_end_at=plan_start_at + timedelta(minutes=30),
            completion_status="done" if actual_start_at else "in_progress",
            actual_start_at=actual_start_at,
        )
    )
    await session.flush()


async def test_fetch_pre_card_notifications_excludes_other_classes_and_no_target(
    real_db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(real_db_session)
    action_item_id = await _seed_action_item(real_db_session, user_id=user_id)
    await _seed_notification(
        real_db_session,
        user_id=user_id,
        notification_class="pre_card",
        target_action_item_id=action_item_id,
    )
    await _seed_notification(
        real_db_session,
        user_id=user_id,
        notification_class="pre_card",
        target_action_item_id=None,  # target 없음 — 분모에서 빠져야 한다
    )
    await _seed_notification(
        real_db_session,
        user_id=user_id,
        notification_class="evening_reflection",
        target_action_item_id=None,
    )

    rows = await _fetch_pre_card_notifications(real_db_session)

    matching = [r for r in rows if r.action_item_id == action_item_id]
    assert len(matching) == 1
    assert matching[0].sent_at == _SENT_AT


async def test_fetch_actual_starts_excludes_null_and_scopes_to_requested_ids(
    real_db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(real_db_session)
    started = await _seed_action_item(real_db_session, user_id=user_id)
    not_started = await _seed_action_item(real_db_session, user_id=user_id)
    unrequested = await _seed_action_item(real_db_session, user_id=user_id)

    start_time = _SENT_AT + timedelta(minutes=10)
    await _seed_execution(
        real_db_session, user_id=user_id, action_item_id=started, actual_start_at=start_time
    )
    await _seed_execution(
        real_db_session, user_id=user_id, action_item_id=not_started, actual_start_at=None
    )
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=unrequested,
        actual_start_at=_SENT_AT + timedelta(minutes=20),
    )

    starts = await _fetch_actual_starts(real_db_session, {started, not_started})

    assert starts == {started: [start_time]}
    assert unrequested not in starts
