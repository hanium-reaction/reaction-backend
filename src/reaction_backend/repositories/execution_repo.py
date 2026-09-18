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

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import ColumnElement, func, or_, select, update
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

# 카드를 **끝냈다**고 말하는 체크인 값 — 남은 세션 블록을 정리하고 pre_card 알림을 멈추는 기준.
# partial_done/failed 는 "아직 남았다" 라 남은 세션을 그대로 둔다(다음 세션에 이어서 한다).
_CARD_DONE_STATUSES = ("done", "over_done")


def settle_pause(
    execution: ExecutionEvent,
    pause: InterruptionEvent,
    *,
    now: datetime,
    resumed: bool,
) -> None:
    """정지 1건을 마감하고 그 시간을 실행의 `pause_total_minutes` 에 더한다.

    [▶ 계속](`resumed=True`)과 정지 중 체크인(`resumed=False`, today-11)이 같이 쓴다.
    `resumed_after_interrupt` 는 **비어 있을 때만** 채운다 — 6h cron 이 이미 False('6시간 안에
    안 돌아옴')로 적었으면 그 사실은 그대로 두고, 지연분만 채워 넣는다(sched-14).
    """
    minutes = max(int((now - pause.created_at).total_seconds() // 60), 0)
    pause.resume_delay_minutes = minutes
    if pause.resumed_after_interrupt is None:
        pause.resumed_after_interrupt = resumed
    execution.pause_total_minutes += minutes


def reflectable_from() -> ColumnElement[datetime]:
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

    async def action_ids_with_history(
        self, user_id: UUID, action_item_ids: Sequence[UUID]
    ) -> set[UUID]:
        """이 카드들 중 **실행 이력이 하나라도 있는** 것들의 id (#214).

        취소 가능 판정의 세 번째 조건이다. `status` 만 봐서는 부족하다 — 시작했다가
        되돌아와 `planned` 로 남은 카드도 있고, 그건 '없던 일' 이 아니다.

        카드마다 부르면 오늘 어젠다에서 N+1 이 되므로 **한 번에** 묻는다.
        빈 목록이면 쿼리하지 않는다 — `IN ()` 는 PostgreSQL 문법 오류다.
        """
        if not action_item_ids:
            return set()
        stmt = select(ExecutionEvent.action_item_id).where(
            ExecutionEvent.user_id == user_id,
            ExecutionEvent.action_item_id.in_(action_item_ids),
        )
        result = await self._session.execute(stmt)
        return set(result.scalars().all())

    async def latest_execution_ids(
        self, user_id: UUID, action_item_ids: Sequence[UUID]
    ) -> dict[UUID, UUID]:
        """카드 id → **가장 최근 실행 id**.

        FE 가 실패한 카드의 회복 화면에 다시 들어갈 때 필요하다. 예전엔 FE 가 메모리
        맵(`executionIds`)만 보고, 새로고침으로 그게 비면 `POST /today/actions/{id}/start`
        로 **새 실행을 만들어 버렸다** — 그리고 곧바로 failed 로 체크인했다. 그래서 회복
        화면에 한 번 들어갈 때마다 **가짜 실패 기록이 하나씩 늘었다**(실측: 실제로는 두 번
        실패한 카드에 실행 4건). 그 숫자가 주간 리뷰 준수율과 에스컬레이션 레벨을 함께
        밀어 올린다.

        카드마다 부르면 N+1 이므로 한 번에 묻는다. 빈 목록이면 쿼리하지 않는다.
        """
        if not action_item_ids:
            return {}
        stmt = (
            select(ExecutionEvent.action_item_id, ExecutionEvent.id)
            .where(
                ExecutionEvent.user_id == user_id,
                ExecutionEvent.action_item_id.in_(action_item_ids),
            )
            .order_by(ExecutionEvent.action_item_id, ExecutionEvent.created_at)
        )
        rows = (await self._session.execute(stmt)).all()
        # created_at 오름차순이라 같은 카드의 뒤 행이 앞 행을 덮어쓴다 → 마지막 것이 남는다.
        return dict(rows)  # type: ignore[arg-type]

    async def list_active_blocks_for_actions(
        self, user_id: UUID, action_item_ids: Sequence[UUID]
    ) -> list[tuple[UUID, str, datetime, datetime]]:
        """이 카드들에 걸린, 취소되지 않은 블록의 (action_item_id, block_status, start_at, end_at).

        T1 미체크 배지(근거 대장 §6.2, `domain.missed_check_in`)의 재료 — 판정 자체는
        여기서 하지 않는다(`action_cancel` 과 같은 원칙: repo 는 사실만 반환하고,
        "지금 미체크인가"라는 판단은 순수 domain 함수가 한다).

        카드마다 부르면 N+1 이므로 **한 번에** 묻는다. 빈 목록이면 쿼리하지 않는다.
        """
        if not action_item_ids:
            return []
        stmt = select(
            ScheduledBlock.action_item_id,
            ScheduledBlock.block_status,
            ScheduledBlock.start_at,
            # 유예가 블록 길이에 비례하므로(ADR-0009 D5) 끝 시각도 함께 받는다. 카드의
            # `estimated_minutes` 를 쓰지 않는 이유는 `domain.missed_check_in` 참고 —
            # 사용자가 블록 길이를 바꾸면 둘이 갈라지고, 사용자가 보는 건 블록이다.
            ScheduledBlock.end_at,
        ).where(
            ScheduledBlock.user_id == user_id,
            ScheduledBlock.action_item_id.in_(action_item_ids),
            ScheduledBlock.block_status != "cancelled",
        )
        result = await self._session.execute(stmt)
        return [
            (action_item_id, block_status, start_at, end_at)
            for action_item_id, block_status, start_at, end_at in result
        ]

    async def list_carried_over_actions(
        self,
        user_id: UUID,
        *,
        today: date,
        day_start: datetime,
        since: datetime,
        now: datetime,
    ) -> list[ActionItem]:
        """오늘 이전 날짜의 카드 중 **아직 손에서 놓지 않은** 것 — 어젠다가 자정에 놓치던 카드.

        오늘 어젠다는 `target_date == 오늘` 만 보는데, `target_date` 는 블록의 KST 시작일이고
        `plan_scheduler` 는 블록을 자정 너머로도 놓는다(#252). 그래서 23:30 에 시작한 카드는
        00:00 이 되는 순간 오늘 화면에서 사라졌다 — 방금 하던 일을 어디서 완료할지 모르게
        된다(today-3). 둘 중 하나면 이어서 보여준다:

        1. **진행 중 실행**이 회고 창 안에 있다 — 창 기준은 `/reflection/pending` 과 같은
           `reflectable_from() >= since`. 같은 식이어야 "오늘 화면엔 있는데 회고엔 없는"
           (또는 반대) 카드가 안 생기고, 창을 벗어나면 만료 cron 이 정리한다.
        2. 자정을 넘긴 블록이 **아직 안 끝났다**(`start_at < 오늘 0시`, `end_at > now`) —
           23:30~00:30 블록을 00:05 에 늦게라도 시작하려는 경우. 다음 날 세션 블록은
           `start_at` 이 오늘이라 여기 안 걸린다(분할 카드를 끌어오지 않는다).

        보관된 카드는 제외. 날짜 오름차순 → priority 순.
        """
        running = select(ExecutionEvent.action_item_id).where(
            ExecutionEvent.user_id == user_id,
            ExecutionEvent.completion_status == "in_progress",
            reflectable_from() >= since,
        )
        crossing_midnight = select(ScheduledBlock.action_item_id).where(
            ScheduledBlock.user_id == user_id,
            ScheduledBlock.block_status.in_(("scheduled", "started")),
            ScheduledBlock.start_at < day_start,
            ScheduledBlock.end_at > now,
        )
        stmt = (
            select(ActionItem)
            .where(
                ActionItem.user_id == user_id,
                ActionItem.target_date < today,
                ActionItem.archived_at.is_(None),
                or_(ActionItem.id.in_(running), ActionItem.id.in_(crossing_midnight)),
            )
            .order_by(
                ActionItem.target_date.asc(),
                ActionItem.priority.asc(),
                ActionItem.created_at.asc(),
            )
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

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

    async def close_execution(
        self,
        execution: ExecutionEvent,
        *,
        status: str,
        ended_at: datetime,
    ) -> None:
        """실행 1건 종결 — check-in 과 저녁 회고(batch)의 **단일 전이** (today-13).

        예전엔 `POST /today/check-ins` 와 `POST /reflection/batch` 가 같은 쓰기를 각자 복사해
        들고 있었다. 한쪽만 고치면 '집중 화면에서 완료한 기록' 과 '저녁 회고로 완료한 기록' 이
        같은 결과인데 다르게 저장된다 — 취소 블록 가드가 실제로 한쪽에만 먼저 들어갔었다.

        하는 일: completion_status·actual_end_at·actual_duration_minutes + 블록 finished
        + (완료면) 이 카드의 남은 세션 블록 정리.
        `action_item.status` 전이와 회복 완료 스탬프는 **호출자 몫**이다(카드·회복 repo 를
        라우터가 쥔다). commit 도 호출자.
        """
        execution.completion_status = status
        execution.actual_end_at = ended_at
        if execution.actual_start_at is not None:
            delta = ended_at - execution.actual_start_at
            execution.actual_duration_minutes = max(int(delta.total_seconds() // 60), 0)

        block = await self.get_block(execution.scheduled_block_id)
        if block is not None and block.block_status != "cancelled":
            # 취소된 블록은 되살리지 않는다 — 회고 창을 넘겨 만료 cron(#20)이 카드와 함께
            # 정리한 블록에 stale 한 executionId 로 체크인·회고가 들어오면, finished 로 덮어써서
            # 주간 그리드에 유령 블록이 되살아난다(list_week 는 archived 를 안 보고 block_status 만 본다).
            block.block_status = "finished"

        if status in _CARD_DONE_STATUSES:
            await self.cancel_remaining_sessions(execution)

    async def cancel_remaining_sessions(self, execution: ExecutionEvent) -> None:
        """카드를 끝냈으면 **아직 안 한 다른 세션 블록**을 계획에서 뺀다 (critic-2).

        긴 카드는 여러 날의 세션 블록으로 쪼개진다(`plan_scheduler`). 카드 상태는 하나라,
        첫 세션에서 '완료' 를 누르면 카드는 done 인데 둘째 날 블록은 `scheduled` 로 남았다 —
        주간 그리드엔 할 일처럼 계속 뜨고, 5분 전 '곧 시작' 알림까지 왔다. 재계획의 밀린 일
        수거(`list_stale_scheduled_before`)도 카드 상태를 안 봐서 끝낸 카드를 다시 배치하려 한다.

        고르는 블록은 좁다(전부 데이터 보호):
        - `scheduled` 만 — finished(수행 이력)·started(다른 실행이 잡은 블록)는 안 건드린다.
        - `user_edit` 제외 — 사용자가 손으로 옮긴 블록은 시스템이 지우지 않는다(#113 과 같은 선).
        - 방금 끝낸 실행의 블록 제외 — 그건 위에서 finished 가 된다.

        partial_done/failed 는 부르지 않는다 — 남은 세션이 다음에 이어서 할 자리다.
        카드 `status` 는 건드리지 않는다(호출자의 기존 체크인 전이 그대로, AGENTS §2).
        계획 교체·만료 cron 과 같은 soft 규칙(블록 cancelled, hard delete 없음).
        """
        await self._session.execute(
            update(ScheduledBlock)
            .where(
                ScheduledBlock.user_id == execution.user_id,
                ScheduledBlock.action_item_id == execution.action_item_id,
                ScheduledBlock.id != execution.scheduled_block_id,
                ScheduledBlock.block_status == "scheduled",
                ScheduledBlock.source != "user_edit",
            )
            .values(block_status="cancelled")
            .execution_options(synchronize_session=False)
        )

    # ── pause / resume (interruption_events) — #83 Focus 일시정지/재개 ──
    async def get_open_pause(self, execution_id: UUID) -> InterruptionEvent | None:
        """아직 재개되지 않은(열린) user_pause 구간 — 가장 최근 것.

        열림 = `resume_delay_minutes IS NULL` — 지연분은 [▶ 계속]·체크인이 정지를 닫을 때만
        적는다(`settle_pause`). `resumed_after_interrupt` 는 보지 않는다: 6h cron
        (`interruption_resolver`)이 방치분을 False 로 표시해도 사용자가 [▶ 계속] 을 누르기
        전까지 실행은 **여전히 정지 중**이다. 예전엔 그 행을 닫힌 것으로 봐서, 아침에 멈추고
        저녁에 돌아온 사용자의 [계속] 이 409 `TODAY_NOT_PAUSED` 로 영영 실패했고 그 몇 시간은
        정지 시간에 한 번도 안 들어갔다(today-5, sched-14).
        """
        stmt = (
            select(InterruptionEvent)
            .where(
                InterruptionEvent.execution_id == execution_id,
                InterruptionEvent.interruption_type == "user_pause",
                InterruptionEvent.resume_delay_minutes.is_(None),
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
        - 이미 끝낸(done/over_done) 카드 제외 — 쪼갠 세션의 첫 회차에서 '완료' 하면 남은
          세션 블록은 체크인이 정리하지만(`cancel_remaining_sessions`), 그 전에 남은 블록·
          사용자가 옮긴 블록에 "곧 시작" 이 가지 않게 카드 상태도 본다 (critic-2)
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
                ActionItem.status.notin_(_CARD_DONE_STATUSES),
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

        경계 식은 `reflectable_from()` — 만료 cron(`expire_unreflected`)이 쓰는 것과 **반드시
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
                reflectable_from() >= since,
            )
            .order_by(ExecutionEvent.plan_start_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def expire_unreflected(self, *, before: datetime, archived_at: datetime) -> int:
        """회고 창 밖의 미체크 실행의 카드를 만료. 반환: 만료 카드 수.

        `list_pending_reflection` 의 **정확한 여집합** — 두 쪽 다 `reflectable_from()` 을 기준으로
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
            reflectable_from() < before,
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
