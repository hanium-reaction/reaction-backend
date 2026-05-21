"""Alembic migration env — re:action backend.

수정 사항 (vs alembic default async template):
- target_metadata = Base.metadata (autogenerate 지원)
- sqlalchemy.url 은 alembic.ini 가 아닌 settings.database_url 에서 주입
  (config/.env 의 단일 진실 소스 유지)
- 모델 패키지가 import 되어야 metadata에 반영됨 — db.models 도입 시 여기서 import 추가
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# alembic 은 CWD가 프로젝트 루트라고 가정. src/ 를 path에 추가해서 패키지 import 가능하게.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from reaction_backend.config import get_settings  # noqa: E402
from reaction_backend.db.base import Base  # noqa: E402
from reaction_backend.db.session import normalize_async_url  # noqa: E402

# 후속 모델 추가 시: 모델을 import 해야 Base.metadata 에 등록됨.
# 예: from reaction_backend.db import models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# .env → settings → alembic config 로 URL 주입.
settings = get_settings()
database_url = normalize_async_url(settings.database_url)
if database_url:
    # alembic.ini 의 sqlalchemy.url 은 비워두고, 여기서 동적 set.
    config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Offline mode — URL만으로 SQL emit (실제 연결 없이)."""
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. .env 를 만들고 DATABASE_URL 을 채우거나 "
            "환경변수로 주입한 뒤 다시 시도하세요."
        )
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Online mode — async engine 생성, 연결, 마이그레이션."""
    if not config.get_main_option("sqlalchemy.url"):
        raise RuntimeError(
            "DATABASE_URL is not set. .env 를 만들고 DATABASE_URL 을 채우거나 "
            "환경변수로 주입한 뒤 다시 시도하세요."
        )

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
