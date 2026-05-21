"""DB 연결 통합 테스트.

DATABASE_URL이 설정되지 않으면 skip. CI에서 DB 없이도 통과해야 함.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from reaction_backend.config import get_settings
from reaction_backend.db.session import get_sessionmaker, normalize_async_url

DB_AVAILABLE = bool(get_settings().database_url)


def test_normalize_async_url_postgresql_scheme():
    assert (
        normalize_async_url("postgresql://u:p@host:5432/db")
        == "postgresql+asyncpg://u:p@host:5432/db"
    )


def test_normalize_async_url_already_asyncpg():
    """이미 +asyncpg 가 붙어 있으면 그대로."""
    url = "postgresql+asyncpg://u:p@host/db"
    assert normalize_async_url(url) == url


def test_normalize_async_url_heroku_postgres():
    """Heroku 구식 postgres:// 스킴도 처리."""
    assert normalize_async_url("postgres://u:p@host/db") == "postgresql+asyncpg://u:p@host/db"


def test_normalize_async_url_empty_passthrough():
    assert normalize_async_url("") == ""


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set — integration test skipped")
async def test_async_session_can_select_one():
    """실제 DB 에 async session 으로 SELECT 1 — connectivity 검증."""
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1
