"""Execution repository — S13 Focus 실행 로깅 + S18 실패 태깅 (Issue #19-B).

규칙:
- user_id scope 자동.
- `action_item.status` 전이는 체크인(execution 레이어)의 책임 — ActionItemRepo
  docstring 과 합의된 유일한 변경 지점. 회복(Recovery)은 절대 변경하지 않는다.
- 회고 창 만료 마킹(`system_failure_reason`/`archived_at` + 블록 cancel)은 cron 전용
  (`expire_unreflected`, Issue #20) — 여기서도 `status` 는 불변이다.
- 실패 태그는 1회만 기록 (재태깅 시 409) — hard delete 회피 (AGENTS.md §2).
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.execution_failure_tag import ExecutionFailureTag
from reaction_backend.db.models.failure_reason_tag import FailureReasonTag
from reaction_backend.db.models.interruption_event import InterruptionEvent
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.db.session import get_db


def _reflectable_from() -> ColumnElement[datetime]:
    """실행을 **회고할 수 있게 된 시각** = 계획 시각과 실제 착수 시각 중 나중 (#20).

    회고 창의 단일 기준식 — `list_pending_reflection`(창 안: `>= since`)과
    `expire_unreflected`(창 밖: `< since`)가 **둘 다 이 식을 쓴다**. 그래야 두 집합이
    정확한 여집합이 되어, 어느 쪽에도 안 드는 카드(회고 화면엔 안 뜨는데 만료는 되는 카드)가
    생기지 않는다.

    `plan_start_at` 만 보면 안 되는 이유: `find_open_block` 에 날짜 필터가 없어 지난 블록을
    뒤늦게 [▶시작] 할 수 있고, 그러면 계획 시각은 과거인데 실제 착수는 방금이다. 계획 시각만
    보면 어제 착수한 카드가 오늘 만료되고, 회고 화면엔 애초에 뜨지도 않는다.
    """
    return func.greatest(
        ExecutionEvent.plan_start_at,
        func.coalesce(ExecutionEvent.actual_start_at, ExecutionEvent.plan_start_at),
    )


class ExecutionRepo:
    """ExecutionEvent + ad-hoc ScheduledBlock + ExecutionFailureTag 영속화."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── execution ──
    async def get_by_id(self, user_id: UUID, execution_id: UUID) -> ExecutionEvent | None:
        stmt = select(ExecutionEvent).where(
            ExecutionEvent.id == execution_id,
            ExecutionEvent.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_for_action(
        self, user_id: UUID, action_item_id: UUID
    ) -> ExecutionEvent | None:
        """진행 중(in_progress) 실행 — [▶ 시작] 중복 방지."""
        stmt = select(ExecutionEvent).where(
            ExecutionEvent.user_id == user_id,
            ExecutionEvent.action_item_id == action_item_id,
            ExecutionEvent.completion_status == "in_progress",
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def find_open_block(self, user_id: UUID, action_item_id: UUID) -> ScheduledBlock | None:
        """이 카드의 미종결(scheduled/started) 블록 — 가장 이른 것."""
        stmt = (
            select(ScheduledBlock)
            .where(
                ScheduledBlock.user_id == user_id,
                ScheduledBlock.action_item_id == action_item_id,
                ScheduledBlock.block_status.in_(("scheduled", "started")),
            )
            .order_by(ScheduledBlock.start_at)
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def create_adhoc_block(
        self, *, user_id: UUID, action_item: ActionItem, start_at: datetime
    ) -> ScheduledBlock:
        """블록 없이 시작한 즉석 실행용 블록 (source='user_edit', §5.10)."""
        block = ScheduledBlock(
            user_id=user_id,
            action_item_id=action_item.id,
            start_at=start_at,
            end_at=start_at + timedelta(minutes=action_item.estimated_minutes),
            block_status="started",
            source="user_edit",
        )
        self._session.add(block)
        await self._session.flush()
        await self._session.refresh(block)
        return block

    async def create_execution(
        self,
        *,
        user_id: UUID,
        action_item_id: UUID,
        block: ScheduledBlock,
        started_at: datetime,
    ) -> ExecutionEvent:
        execution = ExecutionEvent(
            user_id=user_id,
            action_item_id=action_item_id,
            scheduled_block_id=block.id,
            plan_start_at=block.start_at,
            plan_end_at=block.end_at,
            actual_start_at=started_at,
            completion_status="in_progress",
        )
        self._session.add(execution)
        await self._session.flush()
        await self._session.refresh(execution)
        return execution

    async def get_block(self, block_id: UUID) -> ScheduledBlock | None:
        stmt = select(ScheduledBlock).where(ScheduledBlock.id == block_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ── pause / resume (interruption_events) — #83 Focus 일시정지/재개 ──
    async def get_open_pause(self, execution_id: UUID) -> InterruptionEvent | None:
        """아직 재개되지 않은(열린) user_pause 구간 — 가장 최근 것.

        열림 = resume_delay_minutes IS NULL AND resumed_after_interrupt IS NULL
        (재개되면 True+지연분, cron 이 방치분을 False 로 마감).
        """
        stmt = (
            select(InterruptionEvent)
            .where(
                InterruptionEvent.execution_id == execution_id,
                InterruptionEvent.interruption_type == "user_pause",
                InterruptionEvent.resume_delay_minutes.is_(None),
                InterruptionEvent.resumed_after_interrupt.is_(None),
            )
            .order_by(InterruptionEvent.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def create_pause(self, *, user_id: UUID, execution_id: UUID) -> InterruptionEvent:
        """[⏸] — user_pause interruption INSERT. created_at 이 정지 시작 시각."""
        row = InterruptionEvent(
            user_id=user_id,
            execution_id=execution_id,
            interruption_type="user_pause",
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def list_blocks_starting_between(
        self, *, start: datetime, end: datetime
    ) -> list[ScheduledBlock]:
        """pre_card 알림 후보 — `[start, end)` 에 시작하는 미착수(`scheduled`) 블록 (#20).

        전 사용자 대상 5분 폴 쿼리라 여기서 바로 거른다:
        - `started` 제외 — 이미 착수한 카드에 "곧 시작" 알림은 소음
        - 카드 archived 제외 — 만료 cron(`expire_unreflected`)이 보관한 카드의 블록은
          cancel 되지만, 블록 상태만 믿지 않고 카드 생사도 본다 (이중 방어)
        - 비활성 사용자 제외 — `UserRepo.list_active()` 와 같은 3조건 (soft-archived·
          익명화 사용자의 잔존 블록에 발송하지 않는다)

        `action_item` 은 payload(카드 제목)용으로 즉시 로드.
        """
        stmt = (
            select(ScheduledBlock)
            .join(ActionItem, ScheduledBlock.action_item_id == ActionItem.id)
            .join(User, ScheduledBlock.user_id == User.id)
            .where(
                ScheduledBlock.block_status == "scheduled",
                ScheduledBlock.start_at >= start,
                ScheduledBlock.start_at < end,
                ActionItem.archived_at.is_(None),
                User.archived_at.is_(None),
                User.is_anonymized.is_(False),
                User.onboarding_state == "ACTIVE",
            )
            .options(joinedload(ScheduledBlock.action_item))
            .order_by(ScheduledBlock.start_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_pending_reflection(
        self, user_id: UUID, *, since: datetime
    ) -> list[ExecutionEvent]:
        """미체크(in_progress) 실행 — 회고 가능 시각 >= since, 오래된 순 (#83 S17 회고).

        시작만 하고 체크인하지 않은 실행 = 저녁 회고에서 소급 처리할 대상.

        경계 식은 `_reflectable_from()` — 만료 cron(`expire_unreflected`)이 쓰는 것과 **반드시
        같아야** 이 창과 만료가 정확한 여집합이 된다(#20). 두 쪽이 서로 다른 컬럼을 보면 어느
        집합에도 안 드는 카드가 생긴다: 지난 블록을 뒤늦게 [▶시작] 하면 `plan_start_at` 은
        이미 창 밖이라 회고 화면에 **한 번도 안 뜨는데**, 만료는 `actual_start_at` 기준이라
        3일 뒤 조용히 보관된다 — 회고 기회 0회로 카드가 사라진다.
        """
        stmt = (
            select(ExecutionEvent)
            .where(
                ExecutionEvent.user_id == user_id,
                ExecutionEvent.completion_status == "in_progress",
                _reflectable_from() >= since,
            )
            .order_by(ExecutionEvent.plan_start_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def expire_unreflected(self, *, before: datetime, archived_at: datetime) -> int:
        """회고 창 밖의 미체크 실행의 카드를 만료. 반환: 만료 카드 수.

        `list_pending_reflection` 의 **정확한 여집합** — 두 쪽 다 `_reflectable_from()` 을 기준으로
        저 쪽이 `>= since` 를 보여주고 이 쪽이 `< since` 를 만료시킨다. 전역(모든 사용자) 일괄 처리 — cron 전용(Issue #20).

        만료 = `system_failure_reason='reflection_skipped'` + `archived_at`(soft delete)
        + 남은 미종결 블록 cancel. 3가지를 **건드리지 않는다**:

        1. `action_item.status` — AGENTS.md §2 (Resilience 지표 전제).
        2. `execution_events.completion_status` — `review_repo.collect_execution_stats` 에
           archived 필터가 없어, 만료 카드의 실행을 주간 KPI 에서 빼주는 유일한 장치가
           `weekly_review._TERMINAL_STATUSES` 의 in_progress 제외다. 'failed' 로 바꾸면
           그 격리가 뚫려 adherence·resilience 가 오염된다.
        3. 이미 `system_failure_reason` 이 있는 카드 — 최초 사유를 보존한다(덮어쓰기 금지).
           멱등성과는 **별개 목적**.

        대상을 좁히는 조건 2개는 **사용자 데이터 보호**가 목적이다 (둘 다 제거 금지):

        - `greatest(plan_start_at, actual_start_at)` — 지난 블록을 뒤늦게 [▶시작] 하면
          `plan_start_at` 은 과거인데 실제 착수는 방금이다(`find_open_block` 에 날짜 필터가
          없어 가능). 계획 시각만 보면 **어제 착수한 카드가 오늘 만료**된다.
        - 창 안/이후에 미종결 블록이 남은 카드는 제외 — 카드 1장이 여러 날짜의 세션 블록을
          가질 수 있다(`ScheduledBlock` docstring, `plan_scheduler` 가 긴 카드를 분할).
          첫 세션만 하고 체크인을 잊었다고 **아직 오지 않은 세션까지 취소**하면 사용자가
          하려던 계획이 조용히 사라진다. 모든 블록이 창 뒤로 지나간 카드만 만료한다.

        ⚠️ 멱등성 비대칭 — `PlanDraftRepo.expire_stale` 은 구동 조건(status='draft')과 전이
        대상이 같은 컬럼이라 멱등이 공짜지만, 여기선 구동 조건(`completion_status='in_progress'`)
        을 위 2번 때문에 영원히 안 바꾼다. 따라서 `ActionItem.archived_at IS NULL` 가드가
        멱등성의 **유일한 방어선**이다 — 제거하면 매일 archived_at 이 갱신되는 비멱등 cron 이
        된다 (AGENTS.md §2 "cron 을 idempotent 하지 않게 작성하지 않는다").

        (성능) 서브쿼리는 execution_events 전역 스캔 — plan_start_at/completion_status 에
        인덱스가 없다. 하루 1회 04:00 단발 + MVP 규모라 수용. 필요 시 partial index 는 별도
        마이그레이션 이슈(AGENTS.md §8).
        """
        unreflected = select(ExecutionEvent.action_item_id).where(
            ExecutionEvent.completion_status == "in_progress",
            _reflectable_from() < before,
        )
        # 창 안/이후에 아직 미종결 블록이 남았다면 그 카드는 '진행 중인 계획' — 만료 대상 아님.
        has_live_block = (
            select(ScheduledBlock.id)
            .where(
                ScheduledBlock.action_item_id == ActionItem.id,
                ScheduledBlock.block_status.in_(("scheduled", "started")),
                ScheduledBlock.start_at >= before,
            )
            .exists()
        )
        expire_cards = (
            update(ActionItem)
            .where(
                ActionItem.archived_at.is_(None),
                ActionItem.system_failure_reason.is_(None),
                ActionItem.id.in_(unreflected),
                ~has_live_block,
            )
            .values(system_failure_reason="reflection_skipped", archived_at=archived_at)
            .returning(ActionItem.id)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(expire_cards)
        expired_ids = list(result.scalars().all())
        if not expired_ids:
            return 0

        # 카드가 사라져도 블록이 남으면 주간 그리드(list_week)에 유령 블록이 뜬다 —
        # list_week 는 archived 를 안 보고 block_status != 'cancelled' 만 보기 때문.
        # 승인=교체(supersede) 가 카드 archived + 블록 cancelled 를 짝으로 처리하는 것과 같다.
        # 남은 블록은 위 `has_live_block` 가드 때문에 전부 창 뒤(과거)다 — 미래 세션은 안 지운다.
        # 단 **미종결 블록만** — finished 블록은 실제 수행 이력이라 취소하면 기록이 왜곡된다.
        cancel_blocks = (
            update(ScheduledBlock)
            .where(
                ScheduledBlock.action_item_id.in_(expired_ids),
                ScheduledBlock.block_status.in_(("scheduled", "started")),
            )
            .values(block_status="cancelled")
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(cancel_blocks)
        return len(expired_ids)

    # ── failure tags ──
    async def list_active_failure_tags(self) -> list[FailureReasonTag]:
        stmt = (
            select(FailureReasonTag)
            .where(FailureReasonTag.is_active.is_(True))
            .order_by(FailureReasonTag.sort_order)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def has_failure_tags(self, execution_id: UUID) -> bool:
        stmt = select(ExecutionFailureTag.id).where(
            ExecutionFailureTag.execution_id == execution_id
        )
        result = await self._session.execute(stmt)
        return result.scalars().first() is not None

    async def add_failure_tags(
        self,
        *,
        execution_id: UUID,
        tag_codes: list[str],
        memo_encrypted: str | None,
    ) -> list[ExecutionFailureTag]:
        rows = [
            ExecutionFailureTag(
                execution_id=execution_id,
                tag_code=code,
                memo_encrypted=memo_encrypted,
            )
            for code in tag_codes
        ]
        for row in rows:
            self._session.add(row)
        await self._session.flush()
        return rows


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_execution_repo(session: SessionDep) -> ExecutionRepo:
    return ExecutionRepo(session)
