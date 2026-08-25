"""비싼 엔드포인트 사용자별 일일 호출 한도 (#325).

이 테스트가 못 박는 것:
- `llm_runs` 에 쌓인 실제 행 수(성공·룰 폴백 무관)로 카운트한다.
- module 별로 독립 — planning 이 꽉 차도 recovery 는 안 막힌다.
- 사용자별로 분리, KST 오늘 자만 센다.
- 한도 0 은 무제한(다른 예산 가드들과 같은 관례).
- `enforce()` 는 초과 시 `ApiError`(429, `RATE_LIMIT_DAILY_CALLS_EXCEEDED`)로 변환하고
  `Retry-After` 헤더에 다음 KST 자정까지 남은 초를 싣는다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.config import get_settings
from reaction_backend.db.models.llm_run import LlmRun
from reaction_backend.db.models.user import User
from reaction_backend.safety.endpoint_rate_limit import (
    EndpointCallLimitExceeded,
    check,
    enforce,
    seconds_until_kst_midnight,
)
from reaction_backend.schemas.common import KST, now_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode

pytestmark = pytest.mark.usefixtures("real_db_session")


@pytest.fixture(autouse=True)
def _pin_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`test_grounding_budget.py` 와 같은 관례 — 환경설정이 0(무제한)으로 오버라이드돼
    있어도 이 파일의 산술(`limit * N`)이 깨지지 않게 알려진 값으로 고정한다."""
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_endpoint_daily_call_limit", 3, raising=False)


def _run(user_id: uuid.UUID, *, module: str = "planning", days_ago: int = 0) -> LlmRun:
    row = LlmRun(
        user_id=user_id,
        module=module,
        model="gemini-3.5-flash-lite",
        prompt_id="planning/goal_decompose",
        prompt_version="1",
        tokens_in=100,
        tokens_out=200,
        latency_ms=1_000,
        cost_cents=0,
        cost_micro_usd=10,
        success=True,
        fell_back=False,
    )
    if days_ago:
        row.created_at = now_kst() - timedelta(days=days_ago)
    return row


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="rate limit 테스트"))
    await session.flush()
    return user_id


async def _seed(session: AsyncSession, *rows: LlmRun) -> None:
    for r in rows:
        session.add(r)
    await session.flush()


async def test_allows_until_the_limit(real_db_session: AsyncSession) -> None:
    """경계값 — 한도(3) 직전(2건)까지는 통과. `>` 가 `>=` 로 바뀌면 여기서 죽는다."""
    user_id = await _seed_user(real_db_session)
    await _seed(real_db_session, _run(user_id), _run(user_id))
    await check(real_db_session, user_id=user_id, module="planning")  # raise 없음


async def test_blocks_at_the_limit(real_db_session: AsyncSession) -> None:
    user_id = await _seed_user(real_db_session)
    await _seed(real_db_session, _run(user_id), _run(user_id), _run(user_id))

    with pytest.raises(EndpointCallLimitExceeded) as exc:
        await check(real_db_session, user_id=user_id, module="planning")
    assert exc.value.used == 3
    assert exc.value.limit == 3
    assert exc.value.module == "planning"


async def test_modules_are_independent(real_db_session: AsyncSession) -> None:
    """planning 이 꽉 차도 recovery 는 별도 카운터라 안 막힌다."""
    user_id = await _seed_user(real_db_session)
    await _seed(real_db_session, *[_run(user_id, module="planning") for _ in range(3)])

    with pytest.raises(EndpointCallLimitExceeded):
        await check(real_db_session, user_id=user_id, module="planning")
    await check(real_db_session, user_id=user_id, module="recovery")  # 안 막힘


async def test_users_are_independent(real_db_session: AsyncSession) -> None:
    a = await _seed_user(real_db_session)
    b = await _seed_user(real_db_session)
    await _seed(real_db_session, *[_run(a) for _ in range(3)])

    with pytest.raises(EndpointCallLimitExceeded):
        await check(real_db_session, user_id=a, module="planning")
    await check(real_db_session, user_id=b, module="planning")  # 다른 사용자 — 안 막힘


async def test_only_counts_today(real_db_session: AsyncSession) -> None:
    """어제 호출은 오늘 카운트에 안 들어간다 — KST 자정 경계."""
    user_id = await _seed_user(real_db_session)
    await _seed(real_db_session, *[_run(user_id, days_ago=1) for _ in range(5)])
    await check(real_db_session, user_id=user_id, module="planning")  # 안 막힘


async def test_zero_limit_means_unlimited(real_db_session: AsyncSession) -> None:
    settings = get_settings()
    original = settings.llm_endpoint_daily_call_limit
    settings.llm_endpoint_daily_call_limit = 0
    try:
        user_id = await _seed_user(real_db_session)
        await _seed(real_db_session, *[_run(user_id) for _ in range(10)])
        await check(real_db_session, user_id=user_id, module="planning")  # 안 막힘
    finally:
        settings.llm_endpoint_daily_call_limit = original


async def test_enforce_converts_to_429_with_retry_after(real_db_session: AsyncSession) -> None:
    user_id = await _seed_user(real_db_session)
    await _seed(real_db_session, _run(user_id), _run(user_id), _run(user_id))

    with pytest.raises(ApiError) as exc:
        await enforce(real_db_session, user_id=user_id, module="planning")

    err = exc.value
    assert err.code == ErrorCode.RATE_LIMIT_DAILY_CALLS_EXCEEDED
    assert err.http_status == 429
    assert err.headers is not None
    assert 0 < int(err.headers["Retry-After"]) <= 24 * 3600


def test_seconds_until_kst_midnight_at_end_of_day() -> None:
    almost_midnight = datetime(2026, 8, 25, 23, 59, 30, tzinfo=KST)
    assert seconds_until_kst_midnight(almost_midnight) == 30


def test_seconds_until_kst_midnight_just_after_midnight() -> None:
    just_after = datetime(2026, 8, 25, 0, 0, 1, tzinfo=KST)
    assert seconds_until_kst_midnight(just_after) == 24 * 3600 - 1
