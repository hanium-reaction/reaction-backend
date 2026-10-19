from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.reentry_attempt import ReentryAttempt
from reaction_backend.db.session import get_db


class ReentryRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: UUID, attempt_id: UUID) -> ReentryAttempt | None:
        return (
            await self.session.execute(
                select(ReentryAttempt)
                .where(ReentryAttempt.id == attempt_id, ReentryAttempt.user_id == user_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def active(self, user_id: UUID, action_id: UUID) -> ReentryAttempt | None:
        return (
            await self.session.execute(
                select(ReentryAttempt)
                .where(
                    ReentryAttempt.user_id == user_id,
                    ReentryAttempt.action_item_id == action_id,
                    ReentryAttempt.ended_at.is_(None),
                )
                .order_by(ReentryAttempt.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def add(self, attempt: ReentryAttempt) -> None:
        self.session.add(attempt)
        await self.session.flush()


def get_reentry_repo(session: Annotated[AsyncSession, Depends(get_db)]) -> ReentryRepo:
    return ReentryRepo(session)
