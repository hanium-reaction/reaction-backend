"""PlanDraft repository — First Plan HITL Draft 영속화 (#62).

규칙:
- generate 가 INSERT(create), GET/approve 가 user-scope 조회(get_by_id).
- 만료(72h, ADR-0005 §7.8)는 status 전이로 표현 — `expire_stale` cron 이 일괄 처리(멱등).
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.plan_draft import PlanDraft
from reaction_backend.db.session import get_db


class PlanDraftRepo:
    """PlanDraft 영속화 (생성 / 조회 / 승인 / 만료)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        user_id: UUID,
        *,
        target_date: date,
        horizon: str | None,
        ai_source: str,
        payload: dict[str, Any],
        expires_at: datetime,
    ) -> PlanDraft:
        draft = PlanDraft(
            user_id=user_id,
            target_date=target_date,
            horizon=horizon,
            ai_source=ai_source,
            payload=payload,
            expires_at=expires_at,
        )
        self._session.add(draft)
        await self._session.flush()
        await self._session.refresh(draft)
        return draft

    async def get_by_id(self, user_id: UUID, draft_id: UUID) -> PlanDraft | None:
        """user-scope 단건 조회 (상태 무관 — 호출자가 status/만료 판단)."""
        stmt = select(PlanDraft).where(
            PlanDraft.id == draft_id,
            PlanDraft.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_approved(self, draft: PlanDraft, *, approved_at: datetime) -> PlanDraft:
        draft.status = "approved"
        draft.approved_at = approved_at
        await self._session.flush()
        return draft

    async def expire_stale(self, *, now: datetime) -> int:
        """만료 시각이 지난 draft 를 status='expired' 로 일괄 전이. 반환: 전이된 행 수.

        멱등 — 이미 approved/expired 인 행은 건드리지 않는다(WHERE status='draft').
        """
        stmt = (
            update(PlanDraft)
            .where(PlanDraft.status == "draft", PlanDraft.expires_at < now)
            .values(status="expired")
        )
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]  # CursorResult (UPDATE)


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_plan_draft_repo(session: SessionDep) -> PlanDraftRepo:
    return PlanDraftRepo(session)
