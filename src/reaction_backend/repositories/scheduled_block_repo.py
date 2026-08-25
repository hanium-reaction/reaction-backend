"""ScheduledBlock repository — S14 주간 그리드 / S15 직접 편집 (Issue #21-B).

규칙:
- user_id scope 자동.
- 주간 조회는 action_items 와 join 해 (블록, 제목, 카테고리) 를 함께 반환.
  cancelled 블록(계획 교체로 취소 등)은 그리드에 표시하지 않으므로 제외.
- 충돌 검사는 자기 자신과 cancelled 블록을 제외한 시간 겹침.
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.session import get_db


class ScheduledBlockRepo:
    """ScheduledBlock 주간 조회 + 단건 조회 + 충돌 후보."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_week(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[tuple[ScheduledBlock, str, str, UUID | None]]:
        """[start_dt, end_dt) 의 블록을 (블록, action 제목, 카테고리, goal_id) 로 — start_at 오름차순.

        goal_id 는 블록이 매달린 action_item 의 goal FK — 주간 그리드가 블록을 목표와
        연결(분류/색상)할 수 있게 함께 내려준다. 목표 미연결 액션(inbox 등)은 None.
        cancelled 블록(계획 교체로 취소 등)은 제외 — 취소 이력은 남되 그리드엔 안 보인다.
        """
        stmt = (
            select(ScheduledBlock, ActionItem.title, ActionItem.category, ActionItem.goal_id)
            .join(ActionItem, ScheduledBlock.action_item_id == ActionItem.id)
            .where(
                ScheduledBlock.user_id == user_id,
                ScheduledBlock.block_status != "cancelled",
                ScheduledBlock.start_at >= start_dt,
                ScheduledBlock.start_at < end_dt,
            )
            .order_by(ScheduledBlock.start_at)
        )
        result = await self._session.execute(stmt)
        return [
            (block, title, category, goal_id) for block, title, category, goal_id in result.all()
        ]

    async def get_block(self, user_id: UUID, block_id: UUID) -> ScheduledBlock | None:
        stmt = select(ScheduledBlock).where(
            ScheduledBlock.id == block_id,
            ScheduledBlock.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_action_item(
        self, user_id: UUID, action_item_id: UUID
    ) -> list[ScheduledBlock]:
        """특정 ActionItem 의 블록 (cancelled 제외) — replan 멱등 체크용 (#20-B)."""
        stmt = (
            select(ScheduledBlock)
            .where(
                ScheduledBlock.user_id == user_id,
                ScheduledBlock.action_item_id == action_item_id,
                ScheduledBlock.block_status != "cancelled",
            )
            .order_by(ScheduledBlock.start_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def create_block(
        self,
        *,
        user_id: UUID,
        action_item_id: UUID,
        start_at: datetime,
        end_at: datetime,
        source: str,
    ) -> ScheduledBlock:
        """새 시간 블록 생성 (replan 회복 배치 — source='recovery', #20-B).

        commit 은 호출자 책임.
        """
        block = ScheduledBlock(
            user_id=user_id,
            action_item_id=action_item_id,
            start_at=start_at,
            end_at=end_at,
            source=source,
        )
        self._session.add(block)
        await self._session.flush()
        await self._session.refresh(block)
        return block

    async def list_overlapping(
        self,
        user_id: UUID,
        start_dt: datetime,
        end_dt: datetime,
        *,
        exclude_block_id: UUID,
    ) -> list[ScheduledBlock]:
        """[start_dt, end_dt) 와 겹치는 다른 블록 (자기 자신·cancelled 제외)."""
        stmt = select(ScheduledBlock).where(
            ScheduledBlock.user_id == user_id,
            ScheduledBlock.id != exclude_block_id,
            ScheduledBlock.block_status != "cancelled",
            ScheduledBlock.start_at < end_dt,
            ScheduledBlock.end_at > start_dt,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_busy_between(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[ScheduledBlock]:
        """[start_dt, end_dt) 와 겹치는 모든 블록 (cancelled 제외) — 재계획 시 회피할 기존 일정.

        First Plan 스케줄러가 이미 승인된 블록을 busy 로 반영해 그 위에 겹쳐 잡지 않게 한다
        (비파괴 fit-around). `list_overlapping` 과 달리 자기 자신 제외 인자가 없다.
        """
        stmt = select(ScheduledBlock).where(
            ScheduledBlock.user_id == user_id,
            ScheduledBlock.block_status != "cancelled",
            ScheduledBlock.start_at < end_dt,
            ScheduledBlock.end_at > start_dt,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_scheduled_between(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[tuple[ScheduledBlock, ActionItem]]:
        """[start_dt, end_dt) 의 **미착수('scheduled')** 블록 + 그 ActionItem — 재계획 재배치 대상.

        시작/완료된 블록은 제외(불변). **사용자가 직접 옮긴 블록(`source='user_edit'`)도 제외**
        — 수동 배치를 재계획이 지우지 않는다(#113 원칙). 각 블록의 `id` 는 approve 시 '교체할
        옛 블록' 으로 payload 에 실려, blanket-cancel 없이 그 블록만 재조정 취소된다(#117).
        """
        stmt = (
            select(ScheduledBlock, ActionItem)
            .join(ActionItem, ScheduledBlock.action_item_id == ActionItem.id)
            .where(
                ScheduledBlock.user_id == user_id,
                ScheduledBlock.block_status == "scheduled",
                ScheduledBlock.source != "user_edit",
                ScheduledBlock.start_at >= start_dt,
                ScheduledBlock.start_at < end_dt,
                ActionItem.archived_at.is_(None),
            )
            .order_by(ScheduledBlock.start_at)
        )
        result = await self._session.execute(stmt)
        return [(block, action) for block, action in result.all()]

    async def list_stale_scheduled_before(
        self, user_id: UUID, before_dt: datetime
    ) -> list[tuple[ScheduledBlock, ActionItem]]:
        """`before_dt` 이전에 시작했어야 하는데 **한 번도 착수 안 된** 블록 + 그 ActionItem.

        `list_scheduled_between` 과 필터가 같고 시간 방향만 반대다(과거). 재계획이 **밀린 일**을
        후보로 집기 위한 것 — 아래 셋의 교집합 밖으로 새어나가던 카드를 회수한다.

        | 조회 경로 | 왜 이 카드를 못 보나 |
        |---|---|
        | `list_scheduled_between` | `start_at >= 다음 주 월요일` — 과거 블록은 대상 밖 |
        | `ActionItemRepo.list_planned_without_block` | 이 카드는 **비-cancelled 블록을 갖고 있어** 백로그 정의에서 빠진다 |
        | `expire_unreflected`(만료 cron) | `execution_events.completion_status='in_progress'` 기준 — **[▶시작] 을 한 번도 안 눌러 execution_event 자체가 없는** 카드는 영원히 안 걸린다 |

        즉 "계획만 세워두고 그냥 안 한" 카드 — **가장 흔한 실패 모드** — 가 재계획 후보에서
        통째로 사라지고 있었다. 그 카드는 `status='planned'` 인 채 영구히 남아 사용자의 목록만
        어지럽힌다.

        `source != 'user_edit'` 를 그대로 지키므로 사용자가 직접 옮긴 블록은 여전히 불변이다
        (#113). 반환 형태를 `list_scheduled_between` 과 맞춰 호출부가 같은 루프로 합류시킨다.
        """
        stmt = (
            select(ScheduledBlock, ActionItem)
            .join(ActionItem, ScheduledBlock.action_item_id == ActionItem.id)
            .where(
                ScheduledBlock.user_id == user_id,
                ScheduledBlock.block_status == "scheduled",
                ScheduledBlock.source != "user_edit",
                ScheduledBlock.start_at < before_dt,
                ActionItem.archived_at.is_(None),
            )
            .order_by(ScheduledBlock.start_at)
        )
        result = await self._session.execute(stmt)
        return [(block, action) for block, action in result.all()]

    async def list_committed_between(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[ScheduledBlock]:
        """[start_dt, end_dt) 의 **확정 일정** — 재계획이 회피할(fit-around) 블록.

        확정 = 이미 **시작/완료된** 블록 + **사용자가 직접 옮긴 블록(`source='user_edit'`)**.
        재배치 대상에서 빠지므로 여기 busy 로 포함해야 새 블록이 그 위에 겹치지 않는다.
        """
        stmt = select(ScheduledBlock).where(
            ScheduledBlock.user_id == user_id,
            ScheduledBlock.block_status != "cancelled",
            or_(
                ScheduledBlock.block_status.in_(("started", "finished")),
                ScheduledBlock.source == "user_edit",
            ),
            ScheduledBlock.start_at < end_dt,
            ScheduledBlock.end_at > start_dt,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_scheduled_block_repo(session: SessionDep) -> ScheduledBlockRepo:
    return ScheduledBlockRepo(session)
