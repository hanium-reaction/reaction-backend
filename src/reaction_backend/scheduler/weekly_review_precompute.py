"""Weekly Review precompute cron job — S21 period_summaries (Issue #21-A).

매주 일요일 03:00 (사용자 timezone) 1회 실행. 해당 주(월~일) 실행/회복을 룰로 집계해
`period_summaries`(period_type='weekly') 1행 upsert. **idempotent** — 같은 (user, 주)
이미 있으면 `force=False` 에서 skip (scheduler/README.md 규약).

⚠️ 본 모듈은 **job 로직**이다. 실제 시각 트리거(일요일 03:00 등록)와 전체 user 순회
wrapper 는 Issue #24 운영준비에서 APScheduler/Arq 로 연결한다 (morning_brief 와 동일).

라우터(GET /reviews/weekly · POST generate)도 이 함수를 재사용한다 — 집계/영속화 단일 소스.
LLM 미사용 (MVP 룰 기반, 한 줄 평 P2 — 이슈 #21).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from reaction_backend.orchestrator.weekly_review import WeeklyKpi, compute_weekly_kpis
from reaction_backend.schemas.common import KST

if TYPE_CHECKING:
    from datetime import date

    from reaction_backend.db.models.period_summary import PeriodSummary
    from reaction_backend.repositories.review_repo import ReviewRepo


def week_start_of(day: date) -> date:
    """그 날이 속한 주의 월요일 (weekday: 월=0)."""
    return day - timedelta(days=day.weekday())


def week_window(week_start: date) -> tuple[datetime, datetime]:
    """주(월~일)의 KST 경계 [월 00:00, 다음 월 00:00)."""
    start_dt = datetime.combine(week_start, time.min, tzinfo=KST)
    return start_dt, start_dt + timedelta(days=7)


async def compute_weekly_review(user_id: UUID, week_start: date, *, repo: ReviewRepo) -> WeeklyKpi:
    """해당 주 실행/회복을 수집해 KPI 만 계산 (영속화 X — GET 읽기 경로 재사용)."""
    start_dt, end_dt = week_window(week_start)
    executions = await repo.collect_execution_stats(user_id, start_dt, end_dt)
    recoveries = await repo.collect_recovery_stats(user_id, start_dt, end_dt)
    return compute_weekly_kpis(executions, recoveries, week_start)


async def run_weekly_review_for_user(
    user_id: UUID,
    week_start: date,
    now_kst_dt: datetime,
    *,
    repo: ReviewRepo,
    force: bool = False,
) -> PeriodSummary:
    """사용자 1명의 주간 리뷰 집계 + 영속화 (idempotent).

    `force=False` 면 이미 있는 주는 그대로 반환(skip). `force=True`(수동 재생성)면
    재집계해 덮어쓴다. commit 은 호출자 책임 (cron wrapper / 라우터).
    """
    if not force:
        existing = await repo.get_weekly(user_id, week_start)
        if existing is not None:
            return existing  # idempotent skip

    kpi = await compute_weekly_review(user_id, week_start, repo=repo)
    return await repo.upsert_weekly(
        user_id=user_id,
        week_start=week_start,
        week_end=week_start + timedelta(days=6),
        kpi=kpi,
        generated_at=now_kst_dt,
    )
