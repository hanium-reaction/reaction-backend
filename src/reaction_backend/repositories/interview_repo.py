"""Interview repository — S02 딥 인터뷰 세션·슬롯 영속화 (#6 배선).

규칙(다른 repo 와 동일):
- user_id scope 자동. commit 은 호출자(라우터) 책임.
- **상태 통짜 저장(JSON) 없음** — `interview_sessions` 스칼라(total_turns·ambiguity_final·
  end_reason) + `interview_slot_answers` 행(slot_key→value)으로 정규화 저장한다. 매 요청 시
  라우터가 이 둘을 읽어 `InterviewState` 로 재조립한다 (ADR-0005 §7.4 어댑터 규약).
- `(session_id, slot_key)` UNIQUE → 같은 슬롯 재질문은 INSERT 아닌 UPDATE(UPSERT).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.interview_session import InterviewSession
from reaction_backend.db.models.interview_slot_answer import InterviewSlotAnswer
from reaction_backend.db.session import get_db


class InterviewRepo:
    """InterviewSession + InterviewSlotAnswer 영속화."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_session(self, user_id: UUID, llm_model: str) -> InterviewSession:
        row = InterviewSession(user_id=user_id, llm_model=llm_model, total_turns=0)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_active_session(self, user_id: UUID) -> InterviewSession | None:
        """user 의 진행 중(end_reason IS NULL) 세션 1개. 단일 활성 세션 enforce 용.

        정상 흐름이면 advisory lock(ADR-0005 §7.6) 덕에 최대 1개지만, 방어적으로 limit(1).
        """
        stmt = (
            select(InterviewSession)
            .where(
                InterviewSession.user_id == user_id,
                InterviewSession.end_reason.is_(None),
            )
            .order_by(InterviewSession.started_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active(self, user_id: UUID, session_id: UUID) -> InterviewSession | None:
        """user 소유 세션 1개. 종료 여부는 호출자가 end_reason 으로 판단."""
        stmt = select(InterviewSession).where(
            InterviewSession.id == session_id,
            InterviewSession.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_slot_answers(self, session_id: UUID) -> list[InterviewSlotAnswer]:
        stmt = select(InterviewSlotAnswer).where(InterviewSlotAnswer.session_id == session_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def upsert_slot_answer(
        self,
        session_id: UUID,
        slot_key: str,
        value: dict[str, Any] | None,
        *,
        is_required: bool,
        clarity_score: float | None = None,
    ) -> None:
        """(session_id, slot_key) UPSERT — 재질문 시 같은 행 UPDATE."""
        stmt = select(InterviewSlotAnswer).where(
            InterviewSlotAnswer.session_id == session_id,
            InterviewSlotAnswer.slot_key == slot_key,
        )
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            self._session.add(
                InterviewSlotAnswer(
                    session_id=session_id,
                    slot_key=slot_key,
                    value=value,
                    clarity_score=clarity_score,
                    is_required=is_required,
                )
            )
        else:
            existing.value = value
            if clarity_score is not None:
                existing.clarity_score = clarity_score

    async def save_progress(
        self,
        session: InterviewSession,
        *,
        total_turns: int,
        ambiguity_final: float,
    ) -> None:
        session.total_turns = total_turns
        session.ambiguity_final = ambiguity_final

    async def finalize(
        self,
        session: InterviewSession,
        *,
        end_reason: str,
        total_turns: int,
        ambiguity_final: float,
    ) -> None:
        session.end_reason = end_reason
        session.total_turns = total_turns
        session.ambiguity_final = ambiguity_final
        session.ended_at = datetime.now(UTC)


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_interview_repo(session: SessionDep) -> InterviewRepo:
    return InterviewRepo(session)
