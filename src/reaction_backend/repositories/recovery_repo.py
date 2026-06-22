"""Recovery repository — S19/S20 (Issue #20-A).

규칙:
- user_id scope 자동 (execution / attempt 조회).
- 원본 `action_item.status` 는 본 repo 가 절대 건드리지 않는다 (AGENTS.md §2).
- commit 은 호출자 책임.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.execution_failure_tag import ExecutionFailureTag
from reaction_backend.db.models.recovery_attempt import RecoveryAttempt
from reaction_backend.db.models.recovery_strategy_catalog import RecoveryStrategyCatalog
from reaction_backend.db.session import get_db


class RecoveryRepo:
    """ExecutionEvent 조회 + RecoveryAttempt 영속화 + 전략 카탈로그."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_execution(self, user_id: UUID, execution_id: UUID) -> ExecutionEvent | None:
        stmt = select(ExecutionEvent).where(
            ExecutionEvent.id == execution_id,
            ExecutionEvent.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_failure_tag_codes(self, execution_id: UUID) -> list[str]:
        stmt = select(ExecutionFailureTag.tag_code).where(
            ExecutionFailureTag.execution_id == execution_id
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_active_strategies(self) -> list[RecoveryStrategyCatalog]:
        stmt = (
            select(RecoveryStrategyCatalog)
            .where(RecoveryStrategyCatalog.is_active.is_(True))
            .order_by(RecoveryStrategyCatalog.display_priority)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_attempts(self, user_id: UUID, execution_id: UUID) -> list[RecoveryAttempt]:
        stmt = (
            select(RecoveryAttempt)
            .where(
                RecoveryAttempt.execution_id == execution_id,
                RecoveryAttempt.user_id == user_id,
            )
            .order_by(RecoveryAttempt.created_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_attempt(self, user_id: UUID, attempt_id: UUID) -> RecoveryAttempt | None:
        stmt = select(RecoveryAttempt).where(
            RecoveryAttempt.id == attempt_id,
            RecoveryAttempt.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_attempt(
        self,
        *,
        user_id: UUID,
        execution_id: UUID,
        option_group: str,
        strategy_type: str,
        suggested_action_text: str,
        trigger_tag: str | None,
        llm_fallback_used: bool,
    ) -> RecoveryAttempt:
        attempt = RecoveryAttempt(
            user_id=user_id,
            execution_id=execution_id,
            recovery_option_group=option_group,
            recovery_strategy_type=strategy_type,
            suggested_action_text=suggested_action_text,
            trigger_tag=trigger_tag,
            llm_fallback_used=llm_fallback_used,
        )
        self._session.add(attempt)
        await self._session.flush()
        await self._session.refresh(attempt)
        return attempt


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_recovery_repo(session: SessionDep) -> RecoveryRepo:
    return RecoveryRepo(session)
