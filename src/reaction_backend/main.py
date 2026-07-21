"""FastAPI 앱 진입점.

re:action backend는 18개 도메인 라우터로 구성된다 (docs/api-contract.md):
  health · auth · onboarding · interview · time_policies · fixed_schedules
  · calendar · notifications · goals · habits · inbox · planning · today
  · reflection · recovery · review · policy · settings

도메인 라우터는 Issue #3 에서 도메인별 mock/stub 으로 채워지는 중이다.
auth·onboarding·interview(#3-B), time_policies·calendar·fixed_schedules·notifications(#3-C),
goals·habits·inbox(#3-D) 구현 완료. 나머지는 placeholder 501.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from reaction_backend.api.deps import get_current_user
from reaction_backend.api.exception_handlers import register_exception_handlers
from reaction_backend.api.middleware.idempotency import IdempotencyMiddleware
from reaction_backend.api.routes import (
    auth,
    calendar,
    fixed_schedules,
    goals,
    habits,
    health,
    inbox,
    interview,
    notifications,
    onboarding,
    planning,
    policy,
    recovery,
    reflection,
    review,
    settings,
    time_policies,
    today,
)
from reaction_backend.config import get_settings

_log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """앱 수명주기 — `SCHEDULER_ENABLED=true` 면 in-process cron 스케줄러 기동 (#24).

    기본 OFF: 테스트/로컬에서는 스케줄러가 안 돈다(데모는 시드로 커버). apscheduler import 도
    enabled 일 때만 (lazy) — 미설치 환경에서도 부팅 가능.
    """
    scheduler = None
    if get_settings().scheduler_enabled:
        from reaction_backend.scheduler.runtime import build_scheduler

        scheduler = build_scheduler()
        scheduler.start()
        _log.info("APScheduler started (%d jobs)", len(scheduler.get_jobs()))
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


def _configure_logging() -> None:
    """앱 로그를 stdout 으로 — 없으면 운영 INFO 로그가 통째로 사라진다.

    Python 기본 root 로거는 핸들러가 없어 `logging.lastResort`(WARNING)로 떨어진다.
    uvicorn 은 `uvicorn.*` 네임스페이스만 설정하므로 `reaction_backend.*` 의 INFO 는
    **아무 데도 안 남는다** — 실제로 그래서 "APScheduler started (N jobs)"(#24 기동 확인)와
    "expire_unreflected: N cards expired"(#20 만료 건수, 사고 시 원복 범위 산정용)가
    라이브에서 보이지 않았다. 둘 다 사후에 "무엇이 일어났나"를 아는 유일한 수단이다.

    `basicConfig` 는 root 에 핸들러가 이미 있으면 no-op 이라 다른 설정을 덮어쓰지 않는다.
    systemd 는 stdout 을 journald 로 수집하므로 `journalctl -u reaction-backend` 로 보인다.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def create_app() -> FastAPI:
    _configure_logging()
    cfg = get_settings()

    app = FastAPI(
        title=cfg.app_name,
        version=cfg.app_version,
        description=(
            "re:action backend — 한이음 프로젝트. "
            "도메인/플로우 명세는 docs/api-contract.md, "
            "에이전트 아키텍처는 docs/architecture.md 참고."
        ),
        lifespan=_lifespan,
    )

    # 전역 예외 핸들러 — 모든 에러를 ErrorResponse 로 직렬화 (ADR-0002 §2.2)
    register_exception_handlers(app)

    # Idempotency-Key 미들웨어 (ADR-0002 §2.3) — CORS 안쪽에 두어
    # 캐시/에러 응답에도 CORS 헤더가 적용되도록 한다.
    app.add_middleware(IdempotencyMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_allow_origins,
        allow_origin_regex=cfg.cors_allow_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # health는 prefix 없이 루트 경로 (인증 X — readiness 신호)
    app.include_router(health.router)

    # 인증 불필요 도메인 — auth는 자체 발급, onboarding/status 는 함수에서 CurrentUser 의존
    app.include_router(auth.router)
    app.include_router(onboarding.router)

    # 인증 필수 도메인 — 모든 endpoint 에 Depends(get_current_user) (#16 DoD)
    authed = [Depends(get_current_user)]
    for r in (
        interview.router,
        time_policies.router,
        goals.router,
        habits.router,
        habits.router_instances,
        inbox.router,
        planning.router,
        calendar.router,
        fixed_schedules.router,
        today.router,
        reflection.router,
        recovery.router,
        review.router,
        policy.router,
        notifications.router,
        settings.router,
        settings.router_privacy,
    ):
        app.include_router(r, dependencies=authed)

    return app


app = create_app()
