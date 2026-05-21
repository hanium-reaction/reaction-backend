"""Health check — 유일한 walking skeleton 구현 엔드포인트.

DB 연결 가능 여부와 latency 를 함께 노출. DB 실패해도 HTTP 200 유지하고
`status="degraded"` 로 표시. (k8s readiness 분리는 추후 도입.)
"""

import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text

from reaction_backend.config import Settings, get_settings
from reaction_backend.db.session import get_engine
from reaction_backend.schemas.common import DbStatus, HealthResponse

router = APIRouter(tags=["health"])

SettingsDep = Annotated[Settings, Depends(get_settings)]

DB_PING_TIMEOUT_SECONDS = 2.0


async def _check_db(database_url: str) -> DbStatus:
    """짧은 timeout으로 SELECT 1 — pool 만들거나 잡지 않고 빠르게."""
    if not database_url:
        return DbStatus(ok=False, error="DATABASE_URL not configured")
    try:
        engine = get_engine()
        start = time.perf_counter()
        async with engine.connect() as conn:
            await asyncio.wait_for(
                conn.execute(text("SELECT 1")),
                timeout=DB_PING_TIMEOUT_SECONDS,
            )
        latency_ms = int((time.perf_counter() - start) * 1000)
        return DbStatus(ok=True, latency_ms=latency_ms)
    except Exception as e:  # noqa: BLE001 — health는 어떤 에러든 잡아 보고
        return DbStatus(ok=False, error=f"{type(e).__name__}: {e}"[:200])


@router.get("/health", response_model=HealthResponse)
async def health(settings: SettingsDep) -> HealthResponse:
    db = await _check_db(settings.database_url)
    return HealthResponse(
        status="ok" if db.ok else "degraded",
        app=settings.app_name,
        version=settings.app_version,
        env=settings.app_env,
        db=db,
    )
