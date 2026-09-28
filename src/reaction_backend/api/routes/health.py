"""Health check — 유일한 walking skeleton 구현 엔드포인트.

DB 연결 가능 여부와 latency 를 함께 노출. DB 에 닿지 못하면 `status="degraded"` 와 함께
**HTTP 503** 을 준다. 예전엔 degraded 도 200 이라, 상태 코드만 보는 외부 업타임 감시·로드
밸런서·Docker HEALTHCHECK 가 DB 장애를 정상으로 읽었다 — 이 앱은 DB 없이 할 수 있는 일이
사실상 없으므로 DB 장애는 곧 서비스 장애다. 본문은 200 일 때와 같은 모양이다.

⚠️ 이 경로는 **인증 없이 공개**다(Caddy 가 그대로 프록시). DB 예외 원문을 응답에 싣지 않는다 —
asyncpg 메시지에는 DB 사용자 이름(`password authentication failed for user "…"`)과 내부 주소
(`Connect call failed ('172.31.x.x', 5432)`)가 들어 있어, 장애 중엔 누구나 그걸 읽을 수 있었다.
응답엔 고정 값만, 원문은 서버 로그에만 남긴다.
"""

import asyncio
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text

from reaction_backend.config import Settings, get_settings
from reaction_backend.db.session import get_engine
from reaction_backend.schemas.common import DbStatus, HealthResponse

router = APIRouter(tags=["health"])

_log = logging.getLogger(__name__)

# 공개 응답에 싣는 고정 오류 값 — 원인은 서버 로그(`health db check failed`)에서 본다.
DB_UNAVAILABLE = "db_unavailable"

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
    except Exception:  # noqa: BLE001 — health는 어떤 에러든 잡아 보고
        _log.warning("health db check failed", exc_info=True)
        return DbStatus(ok=False, error=DB_UNAVAILABLE)


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "DB 에 닿지 못함 (degraded)"}},
)
async def health(settings: SettingsDep, response: Response) -> HealthResponse:
    db = await _check_db(settings.database_url)
    if not db.ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if db.ok else "degraded",
        app=settings.app_name,
        version=settings.app_version,
        env=settings.app_env,
        db=db,
    )
