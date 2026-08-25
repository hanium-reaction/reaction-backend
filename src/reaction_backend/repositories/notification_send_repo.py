"""NotificationSend repository — 발송 이력 조회/기록 (Issue #20 알림 cron).

게이트 enforce 근거 컬럼(`user_id`/`notification_class`/`sent_at`)은 INSERT only —
수정·삭제 메서드를 두지 않는다(`llm_runs` 와 같은 원칙). 기록은 **발송 성공 시에만** —
호출 규약은 `safety/push_gate.py`. commit 은 호출자(sweep) 책임.

`opened_at` 은 예외다 — 게이트 판정과 무관한 별도 컬럼(근거 대장 §6.1)이라 `stamp_opened`
로 최초 1회만 채운다(`recovery_repo.stamp_first_viewed` 와 같은 관례). `POST
/notifications/{notificationId}/opened` 가 이걸 호출하지만, **그 endpoint 를 실제로
부르는 FE 콜백(push `notificationclick` 핸들러)은 아직 없다** — 인프라만 미리 준비해
둔 것이고, FE 가 배선하기 전까지 `opened_at` 은 항상 NULL 이다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.notification_send import NotificationSend
from reaction_backend.db.session import get_db

if TYPE_CHECKING:
    from datetime import datetime


class NotificationSendRepo:
    """발송 이력 영속화 — 사용자 락 + 게이트 조회 2종 + 기록."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_user(self, user_id: UUID) -> None:
        """사용자 단위 advisory lock (트랜잭션 스코프) — 게이트 검사~기록 직렬화.

        evening·pre_card cron 은 같은 5분 틱에 병행 실행되고 dedup·예산 조회는 **커밋된
        행만** 본다 — 직렬화 없이는 두 게이트가 동시에 count=2 를 읽고 둘 다 발송해
        주 ≤3건 잠금을 초과한다 (TOCTOU, ADR-0006 §8). `pg_advisory_xact_lock` 은
        커밋/롤백 시 자동 해제되고 DB 수준이라 다중 인스턴스 간에도 직렬화된다.
        sweep 이 사용자 단위로 commit 하므로 락 보유 구간은 1명 분이다.
        """
        stmt = select(func.pg_advisory_xact_lock(func.hashtext(str(user_id))))
        await self._session.execute(stmt)

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
        self,
        *,
        id: UUID,  # noqa: A002 — 발송 전 호출부가 미리 만든 id (push payload 에 이미 실려 나감)
        user_id: UUID,
        notification_class: str,
        sent_at: datetime,
        target_action_item_id: UUID | None = None,
    ) -> NotificationSend:
        """`id` 는 서버가 이 시점에 새로 발급하지 않는다 — `notify_sweeps.py` 가 payload
        를 만들기 **전**에 미리 생성해 넘긴다. 그래야 브라우저로 나간 push payload 의
        `id` 와 여기 저장되는 행의 PK 가 같아서, 나중에 클라이언트가 "이 알림을 열었다"고
        되돌려줄 때 그 id 로 이 행을 찾을 수 있다 — 발송 **후**에 서버가 id 를 새로
        만들면(예전 `server_default`) 그 값이 payload 에 없어 영원히 못 찾는다.
        """
        row = NotificationSend(
            id=id,
            user_id=user_id,
            notification_class=notification_class,
            sent_at=sent_at,
            target_action_item_id=target_action_item_id,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_by_id(self, notification_id: UUID, user_id: UUID) -> NotificationSend | None:
        stmt = select(NotificationSend).where(
            NotificationSend.id == notification_id,
            NotificationSend.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def stamp_opened(self, notification: NotificationSend, opened_at: datetime) -> None:
        """최초 1회만 채운다 — 재클릭·중복 콜백이 최초 오픈 시각을 덮어쓰지 않는다."""
        if notification.opened_at is None:
            notification.opened_at = opened_at


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_notification_send_repo(session: SessionDep) -> NotificationSendRepo:
    return NotificationSendRepo(session)
