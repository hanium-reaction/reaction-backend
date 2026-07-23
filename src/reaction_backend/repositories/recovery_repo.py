"""Recovery repository — S19/S20 (Issue #20-A).

규칙:
- user_id scope 자동 (execution / attempt 조회).
- 원본 `action_item.status` 는 본 repo 가 절대 건드리지 않는다 (AGENTS.md §2).
- commit 은 호출자 책임.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.execution_failure_tag import ExecutionFailureTag
from reaction_backend.db.models.recovery_attempt import (
    RECOVERY_SUCCESS_STATUSES,
    RecoveryAttempt,
)
from reaction_backend.db.models.recovery_strategy_catalog import RecoveryStrategyCatalog
from reaction_backend.db.session import get_db

if TYPE_CHECKING:
    from datetime import datetime


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

    async def complete_for_action(
        self,
        user_id: UUID,
        action_item_id: UUID,
        *,
        completed_at: datetime,
        completion_status: str,
    ) -> RecoveryAttempt | None:
        """회복 카드의 실행이 종결되면 그 RecoveryAttempt 에 완료 스탬프 (#20).

        **average_recovery_minutes 의 유일한 생산자.** 회복을 채택하면(ADOPTED) 새 카드가
        생기고(`resulting_action_item_id`), 그 카드를 done/over_done 으로 마치면 여기서
        `recovery_completed_at` + `recovery_duration_minutes`(= completed_at −
        `recovery_started_at`, 결정 시각) + `recovery_result='completed'` 를 기록한다.
        `recovery_started_at` 이 결정 시각이므로 duration 은 **결정→회복 완주 경과 시간**이다
        (설계서 §5.16 "종료 시각 − 시작 시각"; CARRY_OVER 는 하루 넘겨 큰 값이 정상).

        failed·partial_done 은 `result='abandoned'` (duration 없음 → 평균에서 제외).
        멱등 — 이미 종결된(`result != 'pending'`) attempt 는 재체크인으로 덮지 않는다.
        `resulting_action_item_id` 는 채택 시에만 채워지므로 그 매칭 자체가 ADOPTED 필터다.

        반환: 스탬프한 attempt (매칭 없거나 이미 종결이면 None — 대다수 카드는 회복이
        아니라 None 이 정상).
        """
        stmt = select(RecoveryAttempt).where(
            RecoveryAttempt.user_id == user_id,
            RecoveryAttempt.resulting_action_item_id == action_item_id,
            RecoveryAttempt.recovery_result == "pending",
        )
        attempt = (await self._session.execute(stmt)).scalar_one_or_none()
        if attempt is None:
            return None

        if completion_status in RECOVERY_SUCCESS_STATUSES:
            attempt.recovery_result = "completed"
            attempt.recovery_completed_at = completed_at
            if attempt.recovery_started_at is not None:
                delta = completed_at - attempt.recovery_started_at
                attempt.recovery_duration_minutes = max(int(delta.total_seconds() // 60), 0)
        else:
            attempt.recovery_result = "abandoned"
        await self._session.flush()
        return attempt


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_recovery_repo(session: SessionDep) -> RecoveryRepo:
    return RecoveryRepo(session)
