"""FastAPI 앱 진입점.

re:action backend는 16개 도메인 라우터로 구성된다 (docs/api-contract.md §1):
  health · auth · onboarding · interview · time_policies · goals · habits
  · planning · calendar · today · reflection · recovery · review · policy
  · notifications · settings

이 walking skeleton 단계에서는 /health 만 실제 구현되어 있고,
나머지는 501 placeholder다. 후속 이슈에서 도메인별로 채워진다.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from reaction_backend.api.routes import (
    auth,
    calendar,
    goals,
    habits,
    health,
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
        planning.router,
        calendar.router,
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
