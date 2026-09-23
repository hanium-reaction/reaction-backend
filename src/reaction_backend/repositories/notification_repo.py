"""Notification settings repository — S08 (Issue #17).

규칙:
- 사용자당 1행 (notification_settings.user_id UNIQUE).
- 없으면 default 값으로 생성 (server_default 와 동일). 동시 첫 접근에도 안전(ON CONFLICT).
- 한 push endpoint(= 한 기기의 브라우저)는 한 사용자에게만 — 마지막으로 구독한 사람에게 간다.
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import time
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
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

        `INSERT … ON CONFLICT (user_id) DO NOTHING` 후 다시 읽는다. 예전 "SELECT → 없으면
        add+flush" 는 새 사용자의 첫 화면에서 요청 두 개(두 탭·연타·GET 과 PATCH 동시)가 함께
        들어오면 둘 다 "없음"을 보고 INSERT 해 뒤쪽이 UNIQUE 위반 500 이 났다. ON CONFLICT 는
        먼저 들어간 행의 커밋을 기다렸다가 조용히 넘어가므로 둘 다 같은 행을 받는다.
        """
        existing = await self.get_by_user(user_id)
        if existing is not None:
            return existing
        await self._session.execute(
            pg_insert(NotificationSetting)
            .values(
                user_id=user_id,
                morning_brief_time=time(8, 0),
                evening_reflection_time=time(21, 0),
                pre_card_enabled=False,
            )
            .on_conflict_do_nothing(index_elements=[NotificationSetting.user_id])
        )
        created = await self.get_by_user(user_id)
        if created is None:  # pragma: no cover — INSERT 또는 경쟁자의 행 중 하나는 반드시 있다
            raise RuntimeError(f"notification_settings row missing after upsert: {user_id}")
        return created

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

        **같은 endpoint 를 가진 다른 사용자의 구독은 지운다.** endpoint 는 기기의 브라우저
        하나를 가리킨다 — 공용 PC·친구 폰에서 A 가 알림을 켜고 로그아웃한 뒤 B 가 같은
        브라우저에서 켜면 같은 endpoint 가 나온다. 둘 다 남겨 두면 A 의 카드 제목이 담긴
        알림이 B 앞에 뜬다. 지금 구독한 사람이 그 기기의 주인이다(soft clear, 행은 남는다).
        """
        endpoint = subscription.get("endpoint")
        if isinstance(endpoint, str) and endpoint:
            await self._session.execute(
                update(NotificationSetting)
                .where(
                    NotificationSetting.user_id != setting.user_id,
                    NotificationSetting.push_subscription["endpoint"].astext == endpoint,
                )
                .values(push_subscription=None)
                .execution_options(synchronize_session=False)
            )
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
