"""Orchestrator 공통 유틸 — user_id × agent 동시성 lock (ADR-0005 §7.6).

한 사용자가 모바일·데스크탑에서 동시에 같은 Agent(예: Interview)에 진입하면 State race
위험이 있다. PostgreSQL **transaction-scoped advisory lock**(`pg_try_advisory_xact_lock`)
으로 막는다 — commit/rollback 시 자동 해제. 본 ADR 결정은 **즉시 fail(409)** +
사용자 재시도 안내 (대기 X).

⚠️ session-level lock(`pg_try_advisory_lock`)을 쓰면 안 되는 이유: 라우터가 lock 컨텍스트
안에서 `session.commit()` 을 호출하면 SQLAlchemy 가 커넥션을 풀로 반환하는데, 이때
session-level lock 은 그 커넥션에 남는다. finally 의 unlock 은 **다른 풀 커넥션**에서
실행될 수 있어 해제에 실패하고, idle 커넥션이 lock 을 영구 보유 → 이후 모든 요청이
409 AGENT_CONCURRENT_ACCESS 로 오탐되는 leak 이 실제 배포에서 발생했다.

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

    ADR-0005 §7.6 구현 — transaction-scoped(`pg_try_advisory_xact_lock`).
    획득 실패 시 `AGENT_CONCURRENT_ACCESS`. 해제는 트랜잭션 종료(commit/rollback)가
    자동 수행하므로 수동 unlock 없음 (xact lock 은 수동 해제 자체가 불가).
    핸들러는 lock 컨텍스트 안에서 마지막에 1회 commit 하는 패턴이므로 임계 구역
    전체가 트랜잭션과 함께 보호된다.
    """
    key = _lock_key(user_id, agent)
    acquired = await session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key})
    if not acquired:
        raise ApiError(
            ErrorCode.AGENT_CONCURRENT_ACCESS,
            "다른 화면에서 진행 중이에요. 잠시 후 다시 시도해주세요.",
            http_status=HTTPStatus.CONFLICT,
        )
    yield
