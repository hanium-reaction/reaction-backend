"""Async DB engine / session 관리.

규약:
- DATABASE_URL 은 표준 `postgresql://...` 형태로 .env 에 둔다.
  (Supabase가 주는 형태 그대로). 이 모듈에서 자동으로 `postgresql+asyncpg://` 로 변환.
- FastAPI 라우터/에이전트는 `Depends(get_db)` 로 세션을 받는다. 직접 sessionmaker import 금지.
- 모든 시간 컬럼은 timestamptz UTC. 응답 시 KST 변환은 schemas 레이어.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from reaction_backend.config import get_settings


def normalize_async_url(url: str) -> str:
    """SQLAlchemy async용 URL로 정규화.

    - 빈 문자열은 그대로 (사용 시점 에러로 surface)
    - `postgres://` (Heroku 구식) → `postgresql://`
    - `postgresql://` → `postgresql+asyncpg://`
    - 이미 driver suffix가 있으면 그대로
    """
    if not url:
        return url
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


@lru_cache
def get_engine() -> AsyncEngine:
    """프로세스 단일 async engine.

    - Supabase Session pooler 친화 옵션:
      * pool_pre_ping: 끊긴 연결 감지
      * pool_recycle: 30분마다 재연결 (긴 idle 방지)
      * statement_cache_size=0 via connect_args: PgBouncer transaction mode 호환
    - echo는 settings.db_echo (local에서 SQL 디버깅용)
    """
    settings = get_settings()
    url = normalize_async_url(settings.database_url)

    return create_async_engine(
        url,
        echo=settings.db_echo,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={
            # asyncpg + PgBouncer (Supabase transaction pooler) 호환.
            # Session pooler 에서도 안전 default.
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
        },
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. 라우터에서 `Annotated[AsyncSession, Depends(get_db)]` 로 사용.

    - 예외 발생 시 자동 rollback
    - 정상 종료 시 자동 close (commit은 호출자 책임)
    """
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """앱 종료 시 engine pool 정리. shutdown event 에서 호출."""
    engine = get_engine()
    await engine.dispose()
