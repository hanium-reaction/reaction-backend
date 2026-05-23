"""FastAPI 앱 진입점.

re:action backend는 18개 도메인 라우터로 구성된다 (docs/api-contract.md):
  health · auth · onboarding · interview · time_policies · fixed_schedules
  · calendar · notifications · goals · habits · inbox · planning · today
  · reflection · recovery · review · policy · settings

도메인 라우터는 Issue #3 에서 도메인별 mock/stub 으로 채워지는 중이다.
auth·onboarding·interview(#3-B), time_policies·calendar·fixed_schedules·notifications(#3-C),
goals·habits·inbox(#3-D) 구현 완료. 나머지는 placeholder 501.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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


def create_app() -> FastAPI:
    cfg = get_settings()

    app = FastAPI(
        title=cfg.app_name,
        version=cfg.app_version,
        description=(
            "re:action backend — 한이음 프로젝트. "
            "도메인/플로우 명세는 docs/api-contract.md, "
            "에이전트 아키텍처는 docs/architecture.md 참고."
        ),
    )

    # 전역 예외 핸들러 — 모든 에러를 ErrorResponse 로 직렬화 (ADR-0002 §2.2)
    register_exception_handlers(app)

    # Idempotency-Key 미들웨어 (ADR-0002 §2.3) — CORS 안쪽에 두어
    # 캐시/에러 응답에도 CORS 헤더가 적용되도록 한다.
    app.add_middleware(IdempotencyMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # health는 prefix 없이 루트 경로
    app.include_router(health.router)

    # 도메인 라우터 — 각 라우터가 자신의 prefix를 가짐
    for r in (
        auth.router,
        onboarding.router,
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
    ):
        app.include_router(r)

    return app


app = create_app()
