"""Privacy repository — S28 즉시 익명화 (Issue #23-B).

사용자의 모든 `*_encrypted` PII 컬럼을 `[anonymized]` sentinel 로 덮어쓴다
(safety.encryption: decrypt 가 sentinel 을 그대로 반환 → 복호화 깨지지 않음).
`users` 플래그/이름 마스킹은 라우터가 ORM 으로 처리. commit 은 호출자 책임.

hard delete 아님 (AGENTS §2) — 행은 보존, 내용만 마스킹.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import Update, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.calendar_connection import CalendarConnection
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.execution_failure_tag import ExecutionFailureTag
from reaction_backend.db.models.inbox_item import InboxItem
from reaction_backend.db.models.interruption_event import InterruptionEvent
from reaction_backend.db.models.llm_run import LlmRun
from reaction_backend.db.session import get_db
from reaction_backend.safety.encryption import ANONYMIZED_SENTINEL

_S = ANONYMIZED_SENTINEL


class PrivacyRepo:
    """사용자 PII(`*_encrypted`) 일괄 마스킹."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _run(self, stmt: Update) -> int:
        result = await self._session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    async def anonymize_user(self, user_id: UUID) -> int:
        """user 소유 암호화 컬럼을 sentinel 로 마스킹. 변경 행 수 반환."""
        masked = 0
        masked += await self._run(
            update(CalendarConnection)
            .where(CalendarConnection.user_id == user_id)
            .values(access_token_encrypted=_S, refresh_token_encrypted=_S)
        )
        masked += await self._run(
            update(ExecutionEvent)
            .where(
                ExecutionEvent.user_id == user_id,
                ExecutionEvent.user_feedback_encrypted.is_not(None),
            )
            .values(user_feedback_encrypted=_S)
        )
        masked += await self._run(
            update(InboxItem).where(InboxItem.user_id == user_id).values(raw_text_encrypted=_S)
        )
        masked += await self._run(
            update(InterruptionEvent)
            .where(
                InterruptionEvent.user_id == user_id,
                InterruptionEvent.interrupt_context_note_encrypted.is_not(None),
            )
            .values(interrupt_context_note_encrypted=_S)
        )
        masked += await self._run(
            update(LlmRun)
            .where(LlmRun.user_id == user_id)
            .values(input_summary_encrypted=_S, output_summary_encrypted=_S)
        )
        # execution_failure_tags 는 user_id 없음 → execution 조인 서브쿼리.
        exec_ids = select(ExecutionEvent.id).where(ExecutionEvent.user_id == user_id)
        masked += await self._run(
            update(ExecutionFailureTag)
            .where(
                ExecutionFailureTag.execution_id.in_(exec_ids),
                ExecutionFailureTag.memo_encrypted.is_not(None),
            )
            .values(memo_encrypted=_S)
        )
        return masked


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_privacy_repo(session: SessionDep) -> PrivacyRepo:
    return PrivacyRepo(session)
