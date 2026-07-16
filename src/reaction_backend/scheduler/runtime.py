"""APScheduler 런타임 — cron job 시각 등록 (Issue #24).

`SCHEDULER_ENABLED=true` 일 때만 앱 lifespan 에서 기동(`main.py`). 기본 OFF —
테스트/로컬은 안 돈다(데모는 시드로 커버).

⚠️ **in-process**(앱과 동일 프로세스). 다중 인스턴스 배포 시 같은 job 이 인스턴스 수만큼
실행된다 — 모든 job 이 idempotent(같은 날/주/대상 재실행 skip)라 결과는 안전하나,
LLM 비용·부하 측면에서 **단일 인스턴스 배포 권장**(PM #24 배포 설정).

시각 기준 = KST. cron 시간표는 `scheduler/README.md`. 등록 대상은 **job 함수가 존재하는 것만**:
morning_brief / weekly_review / interruption_resolver / expire_drafts / expire_reflections.
(anonymize_inactive / habit_instances_generator / notification_dispatcher 등은 job 함수 미구현 → 미등록.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from reaction_backend.db.session import get_sessionmaker
from reaction_backend.repositories.action_item_repo import ActionItemRepo
from reaction_backend.repositories.daily_brief_repo import DailyBriefRepo
from reaction_backend.repositories.execution_repo import ExecutionRepo
from reaction_backend.repositories.interruption_event_repo import InterruptionEventRepo
from reaction_backend.repositories.plan_draft_repo import PlanDraftRepo
from reaction_backend.repositories.review_repo import ReviewRepo
from reaction_backend.repositories.user_repo import UserRepo
from reaction_backend.scheduler import (
    expire_drafts,
    expire_reflections,
    interruption_resolver,
    sweeps,
)
from reaction_backend.schemas.common import KST, now_kst

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession


async def _session_scope() -> AsyncIterator[AsyncSession]:
    """job 1회용 세션 (요청 scope 밖). 예외 시 rollback."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# ── job wrappers — 세션/repo 를 만들어 sweep·전역 job 호출 ──


async def _morning_brief_job() -> None:
    async for session in _session_scope():
        await sweeps.run_morning_brief_sweep(
            now_kst(),
            user_repo=UserRepo(session),
            action_repo=ActionItemRepo(session),
            brief_repo=DailyBriefRepo(session),
            session=session,
        )


async def _weekly_review_job() -> None:
    async for session in _session_scope():
        await sweeps.run_weekly_review_sweep(
            now_kst(),
            user_repo=UserRepo(session),
            review_repo=ReviewRepo(session),
            session=session,
        )


async def _interruption_job() -> None:
    async for session in _session_scope():
        await interruption_resolver.run_interruption_resolver(
            now_kst(), repo=InterruptionEventRepo(session)
        )
        await session.commit()  # resolver 는 외부 commit


async def _expire_drafts_job() -> None:
    async for session in _session_scope():
        await expire_drafts.run_expire_stale_drafts(
            session, now=now_kst(), repo=PlanDraftRepo(session)
        )  # 내부 commit


async def _expire_reflections_job() -> None:
    async for session in _session_scope():
        await expire_reflections.run_expire_unreflected_cards(
            session, now=now_kst(), repo=ExecutionRepo(session)
        )  # 내부 commit


def build_scheduler() -> AsyncIOScheduler:
    """cron job 을 등록한 (미기동) 스케줄러. 호출자가 `.start()`."""
    scheduler = AsyncIOScheduler(timezone=KST)
    scheduler.add_job(
        _morning_brief_job,
        CronTrigger(hour=6, minute=0, timezone=KST),
        id="morning_brief",
        replace_existing=True,
    )
    scheduler.add_job(
        _weekly_review_job,
        CronTrigger(day_of_week="sun", hour=3, minute=0, timezone=KST),
        id="weekly_review",
        replace_existing=True,
    )
    scheduler.add_job(
        _interruption_job,
        CronTrigger(hour="*/6", timezone=KST),
        id="interruption_resolver",
        replace_existing=True,
    )
    scheduler.add_job(
        _expire_drafts_job,
        CronTrigger(hour="*/6", timezone=KST),
        id="expire_drafts",
        replace_existing=True,
    )
    scheduler.add_job(
        _expire_reflections_job,
        CronTrigger(hour=4, minute=0, timezone=KST),
        id="expire_reflections",
        replace_existing=True,
    )
    return scheduler
