"""Review repository — S21 Weekly Review (Issue #21-A).

규칙:
- user_id scope 자동.
- ORM row 를 `orchestrator.weekly_review` 의 순수 dataclass(ExecutionStat/RecoveryStat)
  로 매핑해 반환 — 라우터/cron 은 ORM 비의존, 집계는 순수 함수가 담당.
- commit 은 호출자 책임 (morning_brief / recovery repo 와 동일).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.period_summary import PeriodSummary
from reaction_backend.db.models.recovery_attempt import (
    ADOPTED_DECISION_VALUES,
    RecoveryAttempt,
)
from reaction_backend.db.session import get_db
from reaction_backend.orchestrator.weekly_review import (
    ExecutionStat,
    RecoveryStat,
    WeeklyKpi,
)


class ReviewRepo:
    """PeriodSummary 조회/upsert + 주간 통계 수집."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── period_summaries ──
    async def get_weekly(self, user_id: UUID, week_start: date) -> PeriodSummary | None:
        stmt = select(PeriodSummary).where(
            PeriodSummary.user_id == user_id,
            PeriodSummary.period_type == "weekly",
            PeriodSummary.start_date == week_start,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_weekly(
        self,
        *,
        user_id: UUID,
        week_start: date,
        week_end: date,
        kpi: WeeklyKpi,
        generated_at: datetime,
    ) -> PeriodSummary:
        """주간 요약 INSERT 또는 갱신 (UNIQUE user_id+weekly+start_date 기준 idempotent)."""
        existing = await self.get_weekly(user_id, week_start)
        target = existing or PeriodSummary(
            user_id=user_id,
            period_type="weekly",
            start_date=week_start,
        )
        target.end_date = week_end
        target.adherence_rate = kpi.adherence_rate
        target.consistency_days = kpi.consistency_days
        target.resilience_rate = kpi.resilience_rate
        target.avg_delay_minutes = kpi.avg_delay_minutes
        target.restart_success_rate = kpi.restart_success_rate
        target.repeated_failure_count = kpi.repeated_failure_count
        target.average_recovery_minutes = kpi.average_recovery_minutes
        target.category_success_rate = kpi.category_success_rate
        target.peak_point_window = kpi.peak_point_window
        target.drain_point_window = kpi.drain_point_window
        target.llm_one_liner = kpi.one_liner
        target.policy_update_candidates = kpi.policy_update_candidates
        target.generated_at = generated_at
        if existing is None:
            self._session.add(target)
        await self._session.flush()
        await self._session.refresh(target)
        return target

    # ── 주간 통계 수집 (ORM → 순수 dataclass) ──
    async def collect_execution_stats(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[ExecutionStat]:
        """[start_dt, end_dt) 안의 실행을 카테고리·회복여부와 함께 평탄화."""
        stmt = (
            select(ExecutionEvent, ActionItem.category)
            .join(ActionItem, ExecutionEvent.action_item_id == ActionItem.id)
            .where(
                ExecutionEvent.user_id == user_id,
                ExecutionEvent.plan_start_at >= start_dt,
                ExecutionEvent.plan_start_at < end_dt,
            )
        )
        result = await self._session.execute(stmt)
        rows = list(result.all())
        if not rows:
            return []

        execution_ids = [row[0].id for row in rows]
        recovered_ids = await self._recovered_execution_ids(user_id, execution_ids)

        return [
            ExecutionStat(
                completion_status=ev.completion_status,
                category=category,
                plan_start_at=ev.plan_start_at,
                actual_start_at=ev.actual_start_at,
                delay_minutes=ev.delay_minutes,
                is_recovered=ev.id in recovered_ids,
            )
            for ev, category in rows
        ]

    async def _recovered_execution_ids(self, user_id: UUID, execution_ids: list[UUID]) -> set[UUID]:
        """수락된 회복 카드가 있는 실행 id 집합 (resilience 분자)."""
        stmt = select(RecoveryAttempt.execution_id).where(
            RecoveryAttempt.user_id == user_id,
            RecoveryAttempt.execution_id.in_(execution_ids),
            RecoveryAttempt.user_decision.in_(ADOPTED_DECISION_VALUES),
        )
        result = await self._session.execute(stmt)
        return set(result.scalars().all())

    async def collect_recovery_stats(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[RecoveryStat]:
        """[start_dt, end_dt) 안에 결정된 수락 회복의 소요분 (average_recovery_minutes)."""
        stmt = select(RecoveryAttempt.recovery_duration_minutes).where(
            RecoveryAttempt.user_id == user_id,
            RecoveryAttempt.user_decision.in_(ADOPTED_DECISION_VALUES),
            RecoveryAttempt.recovery_decided_at >= start_dt,
            RecoveryAttempt.recovery_decided_at < end_dt,
        )
        result = await self._session.execute(stmt)
        return [RecoveryStat(recovery_duration_minutes=m) for m in result.scalars().all()]


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_review_repo(session: SessionDep) -> ReviewRepo:
    return ReviewRepo(session)
