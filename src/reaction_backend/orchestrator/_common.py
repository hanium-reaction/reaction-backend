"""Orchestrator 공통 유틸 — user_id × agent 동시성 lock (ADR-0005 §7.6).

한 사용자가 모바일·데스크탑에서 동시에 같은 Agent(예: Interview)에 진입하면 State race
위험이 있다. PostgreSQL **advisory lock** 으로 막는다 (DB 트랜잭션과 무관, 세션 종료 시
자동 해제). 본 ADR 결정은 **즉시 fail(409)** + 사용자 재시도 안내 (대기 X).

적용 대상: Interview · Planning · Recovery (cycle / 트랜잭션 안전). cron 트리거나
짧고 idempotent 한 흐름(Brief · Habit Penalty · Inbox Parser)은 lock 불필요.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.schemas.errors import ApiError, ErrorCode

# pg_advisory_lock 키는 signed 64-bit 정수. user_id × agent 를 안정적으로 해시한다.
_LOCK_KEY_BYTES = 8


def _lock_key(user_id: UUID, agent: str) -> int:
    """`{user_id}:{agent}` → signed bigint advisory lock 키 (프로세스 무관 결정적)."""
    digest = hashlib.sha256(f"{user_id}:{agent}".encode()).digest()
    return int.from_bytes(digest[:_LOCK_KEY_BYTES], "big", signed=True)


@asynccontextmanager
async def user_agent_lock(session: AsyncSession, user_id: UUID, agent: str) -> AsyncIterator[None]:
    """user_id × agent 단위 advisory lock. 동시 진입 시 즉시 409 fail.

    ADR-0005 §7.6 의 `pg_try_advisory_lock(user_id, 'interview')` 결정 구현.
    획득 실패 시 `AGENT_CONCURRENT_ACCESS`, 정상 흐름은 finally 에서 unlock.
    """
    key = _lock_key(user_id, agent)
    acquired = await session.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
    if not acquired:
        raise ApiError(
            ErrorCode.AGENT_CONCURRENT_ACCESS,
            "다른 화면에서 진행 중이에요. 잠시 후 다시 시도해주세요.",
            http_status=HTTPStatus.CONFLICT,
        )
    try:
        yield
    finally:
        await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
