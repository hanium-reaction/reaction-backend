"""Notification settings repository — S08 (Issue #17).

규칙:
- 사용자당 1행 (notification_settings.user_id UNIQUE).
- 없으면 default 값으로 생성 (server_default 와 동일).
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import time
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.notification_setting import NotificationSetting
from reaction_backend.db.session import get_db


class NotificationRepo:
    """NotificationSetting 영속화. 사용자당 1행."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_user(self, user_id: UUID) -> NotificationSetting | None:
        stmt = select(NotificationSetting).where(NotificationSetting.user_id == user_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_or_create(self, user_id: UUID) -> NotificationSetting:
        """없으면 default 값으로 생성 (server_default 와 동일).

        morning 08:00 · evening 21:00 · pre_card_enabled false · push_subscription null.
        """
        existing = await self.get_by_user(user_id)
        if existing is not None:
            return existing
        setting = NotificationSetting(
            user_id=user_id,
            morning_brief_time=time(8, 0),
            evening_reflection_time=time(21, 0),
            pre_card_enabled=False,
        )
        self._session.add(setting)
        await self._session.flush()
        await self._session.refresh(setting)
        return setting

    async def update(
        self,
        setting: NotificationSetting,
        *,
        morning_brief_time: time | None = None,
        evening_reflection_time: time | None = None,
        pre_card_enabled: bool | None = None,
    ) -> NotificationSetting:
        if morning_brief_time is not None:
            setting.morning_brief_time = morning_brief_time
        if evening_reflection_time is not None:
            setting.evening_reflection_time = evening_reflection_time
        if pre_card_enabled is not None:
            setting.pre_card_enabled = pre_card_enabled
        await self._session.flush()
        return setting

    async def set_push_subscription(
        self, setting: NotificationSetting, subscription: dict[str, Any]
    ) -> NotificationSetting:
        """Web Push 구독 객체 저장 — `{endpoint, keys: {p256dh, auth}}`.

        재구독은 덮어쓰기 (1 device 1 subscription 가정, Issue #16 제외범위 문서).
        JSONB 전체 교체라 SQLAlchemy 변경 감지에 안전 (부분 수정 아님).
        """
        setting.push_subscription = subscription
        await self._session.flush()
        return setting

    async def clear_push_subscription(self, setting: NotificationSetting) -> NotificationSetting:
        """구독 해제 — NULL 로 되돌린다 (발송 게이트는 NULL 이면 발송하지 않는다)."""
        setting.push_subscription = None
        await self._session.flush()
        return setting


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_notification_repo(session: SessionDep) -> NotificationRepo:
    return NotificationRepo(session)
