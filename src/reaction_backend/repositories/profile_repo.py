"""Profile repository — Policy Snapshot 레이어(behavioral_profiles / interaction_styles).

지속형 프로필/상호작용 스타일의 user 당 1행(upsert). 온보딩 인터뷰가 write(#A-1),
설정 화면이 read/update(#A-2). 두 테이블 모두 `user_id` UNIQUE.

규칙(memory/README §라이터): 이 레이어만 쓴다. commit 은 호출자 책임.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.behavioral_profile import BehavioralProfile
from reaction_backend.db.models.interaction_style import InteractionStyle
from reaction_backend.db.session import get_db


class ProfileRepo:
    """behavioral_profiles + interaction_styles 조회/upsert (user 당 1행)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── behavioral_profile ──
    async def get_behavioral(self, user_id: UUID) -> BehavioralProfile | None:
        stmt = select(BehavioralProfile).where(BehavioralProfile.user_id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def upsert_behavioral(
        self, user_id: UUID, *, fields: dict[str, Any]
    ) -> BehavioralProfile:
        """행이 없으면 생성, 있으면 갱신. `fields` 의 None 값은 건너뛴다(서버 default 보존)."""
        row = await self.get_behavioral(user_id)
        created = row is None
        if row is None:
            row = BehavioralProfile(user_id=user_id)
        for key, value in fields.items():
            if value is not None:
                setattr(row, key, value)
        if created:
            self._session.add(row)
        await self._session.flush()
        return row

    # ── interaction_style ──
    async def get_interaction(self, user_id: UUID) -> InteractionStyle | None:
        stmt = select(InteractionStyle).where(InteractionStyle.user_id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def upsert_interaction(
        self, user_id: UUID, *, fields: dict[str, Any]
    ) -> InteractionStyle:
        row = await self.get_interaction(user_id)
        created = row is None
        if row is None:
            row = InteractionStyle(user_id=user_id)
        for key, value in fields.items():
            if value is not None:
                setattr(row, key, value)
        if created:
            self._session.add(row)
        await self._session.flush()
        return row


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_profile_repo(session: SessionDep) -> ProfileRepo:
    return ProfileRepo(session)
