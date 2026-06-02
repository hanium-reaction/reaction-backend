"""InterruptionEvent repository — S13 일시정지 + 6h timeout cron (Issue #19-C).

규칙:
- 6h interruption timeout cron 전용 조회/갱신 (`list_stale_unresolved`, `mark_unresumed`).
- pause/resume 생성·갱신은 #19-B Focus 로깅 (execution_events 의존).
- commit 은 호출자(job) 책임.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.interruption_event import InterruptionEvent
from reaction_backend.db.session import get_db


class InterruptionEventRepo:
    """InterruptionEvent 영속화."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_stale_unresolved(self, *, before: datetime) -> list[InterruptionEvent]:
        """`resumed_after_interrupt IS NULL` 이고 `created_at < before` 인 행.

        `before` = now - 6h (호출자가 KST now 기준 계산해 전달).
        """
        stmt = select(InterruptionEvent).where(
            InterruptionEvent.resumed_after_interrupt.is_(None),
            InterruptionEvent.created_at < before,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def mark_unresumed(self, event: InterruptionEvent) -> None:
        """6h 넘게 재개 안 됨 → resumed_after_interrupt=false."""
        event.resumed_after_interrupt = False
        await self._session.flush()


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_interruption_event_repo(session: SessionDep) -> InterruptionEventRepo:
    return InterruptionEventRepo(session)
