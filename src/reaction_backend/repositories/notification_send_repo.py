"""NotificationSend repository — 발송 이력 조회/기록 (Issue #20 알림 cron).

INSERT only — 발송 이력은 게이트 enforce 의 근거라 수정·삭제 메서드를 두지 않는다
(`llm_runs` 와 같은 원칙). 기록은 **발송 성공 시에만** — 호출 규약은 `safety/push_gate.py`.
commit 은 호출자(sweep) 책임.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select

from reaction_backend.db.models.notification_send import NotificationSend

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


class NotificationSendRepo:
    """발송 이력 영속화 — 게이트 조회 2종 + 기록."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_sent_since(self, user_id: UUID, *, since: datetime) -> int:
        """이 사용자에게 `since` 이후 발송된 건수 — **전 클래스 합산** (주 ≤3건 게이트).

        클래스 필터가 없는 것이 계약이다: AGENTS.md §1 "주 ≤ 3건, 3 클래스만"은 클래스별
        예산이 아니라 합산 상한이다 (해석 근거 ADR-0006 §2).
        """
        stmt = select(func.count()).where(
            NotificationSend.user_id == user_id,
            NotificationSend.sent_at >= since,
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def class_sent_since(
        self, user_id: UUID, *, notification_class: str, since: datetime
    ) -> bool:
        """`since` 이후 이 클래스가 이미 발송됐는가 — 같은 클래스 하루 1건 게이트."""
        stmt = select(func.count()).where(
            NotificationSend.user_id == user_id,
            NotificationSend.notification_class == notification_class,
            NotificationSend.sent_at >= since,
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one()) > 0

    async def record(
        self, *, user_id: UUID, notification_class: str, sent_at: datetime
    ) -> NotificationSend:
        row = NotificationSend(
            user_id=user_id,
            notification_class=notification_class,
            sent_at=sent_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row
