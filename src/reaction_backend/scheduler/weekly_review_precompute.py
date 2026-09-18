"""Weekly Review precompute cron job — S21 period_summaries (Issue #21-A).

해당 주(월~일) 실행/회복을 룰로 집계해 `period_summaries`(period_type='weekly') 1행 upsert.
같은 (user, 주) 행이 이미 있으면 `force=False` 는 그대로 두고, `force=True` 는 다시 집계해
덮어쓴다 — 결정적 upsert 라 몇 번 돌아도 결과가 같다(scheduler/README.md 의 idempotent 규약).

**한 주의 숫자는 그 주가 끝나도 바로 확정되지 않는다.** 일요일 카드는 월·화까지 회고할 수
있고(`expire_reflections.PENDING_WINDOW_DAYS`), 늦게 [완료]/[못 함] 을 누르면 그 주의
실행 상태가 바뀐다. 그래서 두 시각을 나눈다:

- 일요일 저녁 폴(`sweeps.run_weekly_review_sweep`) — 진행 중인 주를 `force=True` 로 매번 다시
  집계한다. 예전엔 `force=False` 라 18:00 첫 폴의 스냅샷이 그 주 내내 잠겨, 21:00 회고 알림을
  받고 체크인한 결과가 리포트에 영영 안 들어갔다.
- 회고 창이 닫힌 뒤(`week_final_at`) 한 번 더(`sweeps.run_weekly_review_finalize_sweep`) —
  이 행이 그 주의 **확정본**이다. `GET /reviews/weekly` 는 확정본만 저장값으로 믿고, 그 전에는
  매번 즉석 계산한다(`is_final_summary`).

라우터(GET /reviews/weekly · POST generate)도 이 모듈을 재사용한다 — 집계/영속화 단일 소스.
LLM 미사용 (MVP 룰 기반, 한 줄 평 P2 — 이슈 #21).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from reaction_backend.orchestrator.weekly_review import WeeklyKpi, compute_weekly_kpis
from reaction_backend.scheduler.expire_reflections import PENDING_WINDOW_DAYS
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


def week_final_at(week_start: date) -> datetime:
    """이 주의 숫자가 더 이상 바뀌지 않는 시각 — 다음 주 목요일 00:00 KST.

    일요일 카드는 일·월·화 저녁에 회고할 수 있고 수요일 04:00 에 만료된다
    (`expire_reflections`, 창 `PENDING_WINDOW_DAYS`=3일). 창을 다음 주 월요일부터 3일
    **통째로** 세어 목요일 00:00 으로 둔다 — 하루 넉넉하게 잡아 만료 배치(04:00)와 겹치는
    경계 시각을 피한다.
    """
    start_dt = datetime.combine(week_start, time.min, tzinfo=KST)
    return start_dt + timedelta(days=7 + PENDING_WINDOW_DAYS)


def latest_final_week_start(now_kst_dt: datetime) -> date:
    """`now` 시점에 확정(`week_final_at` 경과)된 가장 최근 주의 월요일.

    목~일에는 지난주, 월~수에는(지난주 창이 아직 열려 있으니) 2주 전이다.
    """
    week_start = week_start_of(now_kst_dt.date()) - timedelta(days=7)
    while week_final_at(week_start) > now_kst_dt:
        week_start -= timedelta(days=7)
    return week_start


def is_final_summary(summary: PeriodSummary, week_start: date) -> bool:
    """이 저장본이 그 주의 확정본인가 — 회고 창이 닫힌 **뒤에** 집계됐는가.

    창이 닫히기 전에 만든 행(일요일 저녁 폴, 수동 generate)은 그 뒤의 늦은 회고를 모른다.
    """
    return summary.generated_at >= week_final_at(week_start)


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
