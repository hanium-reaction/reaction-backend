"""Orchestrator 공통 유틸 — user_id × agent 동시성 lock (ADR-0005 §7.6).

한 사용자가 모바일·데스크탑에서 동시에 같은 Agent(예: Interview)에 진입하면 State race
위험이 있다. PostgreSQL **transaction-scoped advisory lock**(`pg_advisory_xact_lock`)
으로 막는다 — commit/rollback 시 자동 해제.

획득 전략은 **짧은 대기(lock_timeout 5s) 후 409** — ADR-0005 §7.6 이 open question 으로
남긴 "대기 vs 즉시 fail" 을 staging 실측(#76)으로 재검토한 결과다: 인터뷰 턴이 LLM 호출
동안(1~8s) lock 을 점유하는데, FE 중복 발사/더블클릭이 그 창에 겹치면 즉시-fail 은
409 재시도 폭풍을 만든다. 5초 대기는 이런 순간 겹침을 흡수하고, 진짜 장기 점유만
`AGENT_CONCURRENT_ACCESS` 로 fail 한다.

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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.schemas.errors import ApiError, ErrorCode

# pg_advisory_lock 키는 signed 64-bit 정수. user_id × agent 를 안정적으로 해시한다.
_LOCK_KEY_BYTES = 8


def _lock_key(user_id: UUID, agent: str) -> int:
    """`{user_id}:{agent}` → signed bigint advisory lock 키 (프로세스 무관 결정적)."""
    digest = hashlib.sha256(f"{user_id}:{agent}".encode()).digest()
    return int.from_bytes(digest[:_LOCK_KEY_BYTES], "big", signed=True)


def _concurrent_access() -> ApiError:
    return ApiError(
        ErrorCode.AGENT_CONCURRENT_ACCESS,
        "다른 화면에서 진행 중이에요. 잠시 후 다시 시도해주세요.",
        http_status=HTTPStatus.CONFLICT,
    )


def _is_lock_timeout(exc: DBAPIError) -> bool:
    """lock_timeout(SQLSTATE 55P03) 판별 — 그 외 DB 에러는 그대로 전파해야 한다."""
    origin = exc.orig if exc.orig is not None else exc
    text_ = f"{type(origin).__name__} {origin} {getattr(origin, 'sqlstate', '')}".lower()
    return "55p03" in text_ or "lock timeout" in text_ or "locknotavailable" in text_


@asynccontextmanager
async def user_agent_lock(session: AsyncSession, user_id: UUID, agent: str) -> AsyncIterator[None]:
    """user_id × agent 단위 advisory lock — 5s 대기 후 미획득 시 409.

    ADR-0005 §7.6 구현 — transaction-scoped(`pg_advisory_xact_lock`) + `lock_timeout 5s`.
    해제는 트랜잭션 종료(commit/rollback)가 자동 수행하므로 수동 unlock 없음
    (xact lock 은 수동 해제 자체가 불가). 핸들러는 lock 컨텍스트 안에서 마지막에
    1회 commit 하는 패턴이므로 임계 구역 전체가 트랜잭션과 함께 보호된다.

    lock_timeout 은 SET LOCAL(트랜잭션 한정)이라 다른 요청에 영향 없고, 획득 직후
    0(무제한)으로 되돌려 본문 쿼리의 행 lock 대기에는 적용되지 않게 한다.
    """
    key = _lock_key(user_id, agent)
    await session.execute(text("SET LOCAL lock_timeout = '5s'"))
    try:
        # 성공 시 true 반환. 5s 내 미획득이면 55P03 예외.
        acquired = await session.scalar(
            text("SELECT true FROM pg_advisory_xact_lock(:k)"), {"k": key}
        )
    except DBAPIError as exc:
        if _is_lock_timeout(exc):
            raise _concurrent_access() from exc
        raise
    if not acquired:  # 실 PG 는 성공=true/실패=예외 — 테스트 stub(False) 분기용 방어선
        raise _concurrent_access()
    await session.execute(text("SET LOCAL lock_timeout = 0"))
    yield
