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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.execution_failure_tag import ExecutionFailureTag
from reaction_backend.db.models.recovery_attempt import (
    RECOVERY_SUCCESS_STATUSES,
    RecoveryAttempt,
)
from reaction_backend.db.models.recovery_strategy_catalog import RecoveryStrategyCatalog
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.session import get_db

# 회고 창의 단일 기준식 — `abandon_stale` 이 만료 cron 과 **같은 식**을 써야 한다(#20).
from reaction_backend.repositories.execution_repo import reflectable_from

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

    async def abandon_stale(self, *, before: datetime) -> int:
        """회고 창을 벗어났는데 아직 완주 안 된 회복을 포기 처리. 반환: 처리 건수 (#20).

        **왜 필요한가**: `complete_for_action` 은 회복 카드를 **체크인했을 때만** 스탬프한다.
        그래서 (a) 시작만 하고 체크인을 잊은 회복과 (b) **시작조차 안 한** 회복은 영영
        `result='pending'`(아직 진행 중) 으로 남는다 — 특히 (b) 는 execution 이 없어
        `expire_unreflected` 대상도 아니다(그건 `in_progress` 실행만 본다).

        **경계도 기준식도 만료 cron과 같은 단일 소스** — 경계값은
        `pending_reflection_since(오늘)`, 기준식은 `execution_repo.reflectable_from()`.
        예전엔 경계'값'만 공유하고 실제로 재는 **대상**은 `ActionItem.target_date` 였다.
        그 비대칭이 데이터를 파괴했다: `find_open_block` 에 날짜 필터가 없어 지난 카드를
        뒤늦게 [▶시작] 하면 `target_date` 는 창 밖인데 회고는 아직 가능하다. 그 상태에서
        여기가 'abandoned' 로 확정해 버리면, 이후 사용자가 실제로 그 회복을 done 으로
        마쳐도 `complete_for_action` 의 `recovery_result='pending'` 가드에 막혀
        `recovery_duration_minutes` 가 영영 NULL 로 남는다 — **완주한 회복이
        `average_recovery_minutes` 에서 통째로 사라진다**(#20 이 만든 KPI 자체의 손실).

        가드 5개 (전부 데이터 보호 목적):
        1. `recovery_result='pending'` — 멱등. **이미 completed 인 회복을 덮으면 지표가 파괴**된다.
        2. 카드 status 가 완주(done/over_done)면 제외 — `complete_for_action` 이 생기기 전
           데이터나 스탬프를 놓친 경우, 실제로 해낸 회복을 '포기' 로 오염시키지 않는다.
        3. `resulting_action_item_id` 매칭 자체가 채택(ADOPTED) 필터 — 그 컬럼은 채택 시에만 찬다.
        4. **아직 회고할 수 있는 실행이 남은 카드는 제외** — `reflectable_from() >= before`.
           `expire_unreflected` 와 같은 식이라 "사용자가 회고할 수 있는 건 안 건드린다" 가
           두 cron 에서 같은 뜻이 된다.
        5. 경계 이후에 살아있는 블록(scheduled/started)이 남은 카드도 제외 — `_after_block_time`
           이 `plan_start_at` 기준이라 블록 날짜가 `target_date` 와 어긋날 수 있고, S15 이동으로
           일정을 미래로 옮긴 회복도 여기 해당한다. 아직 하기로 되어 있는 회복을 포기로 덮지 않는다.

        ⚠️ `action_item.status` 는 **건드리지 않는다** (AGENTS.md §2) — 포기는
        `recovery_attempts.recovery_result` 에만 기록한다. 전역(모든 사용자) 일괄 — cron 전용.
        """
        # 가드 4 — 회고 창 안의 실행이 하나라도 남았으면 아직 완주할 수 있는 회복이다.
        still_reflectable = (
            select(ExecutionEvent.id)
            .where(
                ExecutionEvent.action_item_id == ActionItem.id,
                reflectable_from() >= before,
            )
            .exists()
        )
        # 가드 5 — 경계 이후에 아직 살아있는 블록이 남은 카드는 '진행 중인 계획'.
        has_live_block = (
            select(ScheduledBlock.id)
            .where(
                ScheduledBlock.action_item_id == ActionItem.id,
                ScheduledBlock.block_status.in_(("scheduled", "started")),
                ScheduledBlock.start_at >= before,
            )
            .exists()
        )
        stale_cards = select(ActionItem.id).where(
            ActionItem.target_date < before.date(),
            ActionItem.status.notin_(RECOVERY_SUCCESS_STATUSES),
            ~still_reflectable,
            ~has_live_block,
        )
        stmt = (
            update(RecoveryAttempt)
            .where(
                RecoveryAttempt.recovery_result == "pending",
                RecoveryAttempt.resulting_action_item_id.in_(stale_cards),
            )
            .values(recovery_result="abandoned")
            .returning(RecoveryAttempt.id)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        return len(list(result.scalars().all()))


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_recovery_repo(session: SessionDep) -> RecoveryRepo:
    return RecoveryRepo(session)
