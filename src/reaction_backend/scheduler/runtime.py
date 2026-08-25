"""APScheduler 런타임 — cron job 시각 등록 (Issue #24).

`SCHEDULER_ENABLED=true` 일 때만 앱 lifespan 에서 기동(`main.py`). 기본 OFF —
테스트/로컬은 안 돈다(데모는 시드로 커버).

⚠️ **in-process**(앱과 동일 프로세스). 다중 인스턴스 배포 시 같은 job 이 인스턴스 수만큼
실행된다 — 기존 job 은 idempotent(같은 날/주/대상 재실행 skip + DB unique 백스톱)라
안전하고, 알림 job 2종은 게이트의 **사용자 advisory lock + 건당 commit**(ADR-0006 §8)이
인스턴스 간 dedup·예산을 직렬화한다. LLM 비용·부하 측면에서 **단일 인스턴스 배포
권장**(PM #24 배포 설정)은 유지.

시각 기준 = KST. cron 시간표는 `scheduler/README.md`. 등록 대상은 **job 함수가 존재하는 것만**:
morning_brief / weekly_review / interruption_resolver / expire_drafts / expire_reflections
/ evening_reflection_notify / pre_card_notify / morning_brief_notify / habit_instances.
(anonymize_inactive 는 job 함수 미구현 → 미등록 (#15).
notification_dispatcher 는 별도 job 이 아니라 발송 게이트로 대체 — ADR-0006 §1.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from reaction_backend.db.session import get_sessionmaker
from reaction_backend.integrations.web_push import get_web_push_sender
from reaction_backend.repositories.action_item_repo import ActionItemRepo
from reaction_backend.repositories.daily_brief_repo import DailyBriefRepo
from reaction_backend.repositories.execution_repo import ExecutionRepo
from reaction_backend.repositories.goal_repo import GoalRepo
from reaction_backend.repositories.habit_instance_repo import HabitInstanceRepo
from reaction_backend.repositories.habit_repo import HabitRepo, current_week_start_kst
from reaction_backend.repositories.interruption_event_repo import InterruptionEventRepo
from reaction_backend.repositories.notification_repo import NotificationRepo
from reaction_backend.repositories.notification_send_repo import NotificationSendRepo
from reaction_backend.repositories.plan_draft_repo import PlanDraftRepo
from reaction_backend.repositories.recovery_repo import RecoveryRepo
from reaction_backend.repositories.review_repo import ReviewRepo
from reaction_backend.repositories.user_repo import UserRepo
from reaction_backend.scheduler import (
    expire_drafts,
    expire_proposed_goals,
    expire_reflections,
    habit_instances,
    interruption_resolver,
    notify_sweeps,
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
            goal_repo=GoalRepo(session),
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
        now = now_kst()
        await expire_reflections.run_expire_unreflected_cards(
            session, now=now, repo=ExecutionRepo(session)
        )  # 내부 commit
        # 같은 경계로 회복 시도도 종결 — 카드만 정리하고 attempt 를 두면 포기한 회복이
        # 영영 'pending'(진행 중)으로 남는다 (#20).
        await expire_reflections.run_abandon_stale_recoveries(
            session, now=now, repo=RecoveryRepo(session)
        )  # 내부 commit
        # 채택조차 안 된(한 번도 안 열어본) 회복 카드도 같은 경계로 닫는다 — 위 abandon_stale
        # 은 채택(ADOPTED) 필터를 거쳐야 만나는데, 이건 그 필터 밖에서 영영 pending 이던 것.
        await expire_reflections.run_expire_undecided_recoveries(
            session, now=now, repo=RecoveryRepo(session)
        )  # 내부 commit


async def _expire_proposed_goals_job() -> None:
    async for session in _session_scope():
        await expire_proposed_goals.run_expire_stale_proposed_goals(
            session, now=now_kst(), repo=GoalRepo(session)
        )  # 내부 commit


async def _evening_reflection_notify_job() -> None:
    async for session in _session_scope():
        await notify_sweeps.run_evening_reflection_notify_sweep(
            now_kst(),
            user_repo=UserRepo(session),
            notif_repo=NotificationRepo(session),
            execution_repo=ExecutionRepo(session),
            send_repo=NotificationSendRepo(session),
            sender=get_web_push_sender(),
            session=session,
        )


async def _morning_brief_notify_job() -> None:
    async for session in _session_scope():
        await notify_sweeps.run_morning_brief_notify_sweep(
            now_kst(),
            user_repo=UserRepo(session),
            notif_repo=NotificationRepo(session),
            recovery_repo=RecoveryRepo(session),
            action_repo=ActionItemRepo(session),
            send_repo=NotificationSendRepo(session),
            sender=get_web_push_sender(),
            session=session,
        )


async def _habit_instances_job() -> None:
    # week_start 는 `GET /today/agenda` 가 읽을 때 쓰는 것과 **같은 함수**로 계산한다 —
    # 생성 쪽과 조회 쪽이 각자 주 경계를 재면 어긋난 주에 행이 생겨 습관이 안 보인다.
    async for session in _session_scope():
        await habit_instances.run_habit_instances_sweep(
            current_week_start_kst(),
            user_repo=UserRepo(session),
            habit_repo=HabitRepo(session),
            instance_repo=HabitInstanceRepo(session),
            session=session,
        )


async def _pre_card_notify_job() -> None:
    async for session in _session_scope():
        await notify_sweeps.run_pre_card_notify_sweep(
            now_kst(),
            execution_repo=ExecutionRepo(session),
            notif_repo=NotificationRepo(session),
            send_repo=NotificationSendRepo(session),
            sender=get_web_push_sender(),
            session=session,
        )


def build_scheduler() -> AsyncIOScheduler:
    """cron job 을 등록한 (미기동) 스케줄러. 호출자가 `.start()`."""
    scheduler = AsyncIOScheduler(timezone=KST)
    scheduler.add_job(
        _morning_brief_job,
        CronTrigger(hour=6, minute=0, timezone=KST),
        id="morning_brief",
        replace_existing=True,
    )
    # 일요일 18~23시 30분 폴 — 예전엔 일요일 03:00 고정 1회였는데, `week_window()` 가 재는
    # 주 경계는 [월 00:00, 다음 월 00:00) 라 03:00 실행 시점엔 그 주 일요일 활동의 대부분이
    # 아직 안 일어난 상태였다(= "주간" 리포트인데 일요일 몫이 거의 빠짐, ADR-0008 §4.1).
    # 18시 이후로 옮기면 그날 활동 대부분이 이미 반영된 채로 집계된다.
    # 고정 1회 대신 폴로 바꾼 이유는 `habit_instances`(runtime.py:211-217)와 같다 — jobstore 가
    # MemoryJobStore 라 이 시간대에 재기동하면 주 1회짜리 job 은 다음 기회 없이 그 주가
    # 통째로 스킵된다. `run_weekly_review_sweep`(scheduler/sweeps.py) 이 일요일 아니면
    # no-op 이라 폴 자체는 월~토에도 등록돼 있지만 매번 즉시 반환한다.
    # misfire_grace_time 은 폴 간격(30분=1800초)보다 짧게 — evening_reflection_notify 와 같은
    # 이유(APScheduler 기본 1초라 발화 시점이 조금만 밀려도 폴이 통째로 skip 된다).
    scheduler.add_job(
        _weekly_review_job,
        CronTrigger(day_of_week="sun", hour="18-23", minute="*/30", timezone=KST),
        id="weekly_review",
        replace_existing=True,
        misfire_grace_time=600,
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
    # 매일 04:00 — 다른 04:00 만료 배치(expire_reflections)와 합류. 잠정 목표 TTL(#178).
    scheduler.add_job(
        _expire_proposed_goals_job,
        CronTrigger(hour=4, minute=0, timezone=KST),
        id="expire_proposed_goals",
        replace_existing=True,
    )
    # 매일 00:05 — 설계서 표기는 "매주 월요일 00:00" 이지만 **일 단위로 돌린다**.
    # 이 job 은 주 1회짜리라 놓치면 다음 기회가 일주일 뒤다. jobstore 가 MemoryJobStore 라
    # 재기동 시 next_run_time 을 now 기준으로 다시 잡는다 — 월요일 00:05 에 앱이 떠 있지
    # 않으면(배포·재부팅) misfire_grace_time 으로도 회수가 안 되고 **그 주 전체가 인스턴스
    # 없이 지나간다**(= 이 job 이 고치려는 바로 그 버그의 재현). 월요일이 아닌 날의 실행은
    # get-or-create 라 no-op 이므로, 일 단위 실행이 재기동 구멍을 하루 안에 자가치유한다.
    # 04:00 배치(만료·집계)와 겹치지 않게 자정 직후에 둔다.
    scheduler.add_job(
        _habit_instances_job,
        CronTrigger(hour=0, minute=5, timezone=KST),
        id="habit_instances",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # 19~23시 5분 폴 — 사용자별 evening_reflection_time(19~23시 설정 가능)을 분 단위로
    # 존중하려면 고정 시각 1회로는 안 된다. 23시대 폴은 게이트(quiet hours)가 전부
    # 막지만, "왜 안 갔는지"가 로그에 남도록 폴 자체는 돌린다.
    # misfire_grace_time: APScheduler 기본 1초라 발화 시점 루프가 1초만 밀려도 폴이
    # 통째로 skip 된다 — 폴 간격(5분) 미만인 60초로 완화.
    scheduler.add_job(
        _evening_reflection_notify_job,
        CronTrigger(hour="19-23", minute="*/5", timezone=KST),
        id="evening_reflection_notify",
        replace_existing=True,
        misfire_grace_time=60,
    )
    # 종일 5분 폴 — [now+2분, now+7분) 시작 블록. 23~07시 발송은 게이트가 막는다.
    # pre_card 는 창이 이동해 skip 폴을 다음 폴이 회수하지 못하므로 grace 가 특히 중요.
    scheduler.add_job(
        _pre_card_notify_job,
        CronTrigger(minute="*/5", timezone=KST),
        id="pre_card_notify",
        replace_existing=True,
        misfire_grace_time=60,
    )
    # 06~10시 5분 폴 — evening_reflection_notify 와 같은 이유(사용자별 morning_brief_time
    # 을 분 단위로 존중). T2(근거 대장 §6.2) — 대상 없는 날은 매 폴 즉시 skip 이라 비용 없음.
    scheduler.add_job(
        _morning_brief_notify_job,
        CronTrigger(hour="6-10", minute="*/5", timezone=KST),
        id="morning_brief_notify",
        replace_existing=True,
        misfire_grace_time=60,
    )
    return scheduler
