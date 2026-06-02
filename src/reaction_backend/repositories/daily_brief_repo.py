"""DailyBrief repository — S10 Morning Brief 캐시 (Issue #19).

규칙:
- (user_id, brief_date) UNIQUE — 하루 1건.
- 조회(get_by_date, #19-A) + 생성(create, #19-C Morning Brief cron).
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.daily_brief import DailyBrief
from reaction_backend.db.session import get_db


class DailyBriefRepo:
    """DailyBrief 영속화 (사용자당 날짜별 1건)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_date(self, user_id: UUID, brief_date: date) -> DailyBrief | None:
        stmt = select(DailyBrief).where(
            DailyBrief.user_id == user_id,
            DailyBrief.brief_date == brief_date,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        user_id: UUID,
        brief_date: date,
        *,
        headline_text: str,
        expires_at: datetime,
        big_rock_action_item_id: UUID | None = None,
        adjustment_hints: list[dict[str, Any]] | None = None,
        fallback_used: bool = False,
    ) -> DailyBrief:
        """Morning Brief cron 전용 INSERT. idempotency(같은 날 skip)는 호출자(job)가 보장."""
        brief = DailyBrief(
            user_id=user_id,
            brief_date=brief_date,
            headline_text=headline_text,
            big_rock_action_item_id=big_rock_action_item_id,
            adjustment_hints=adjustment_hints or [],
            fallback_used=fallback_used,
            expires_at=expires_at,
        )
        self._session.add(brief)
        await self._session.flush()
        await self._session.refresh(brief)
        return brief


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_daily_brief_repo(session: SessionDep) -> DailyBriefRepo:
    return DailyBriefRepo(session)
