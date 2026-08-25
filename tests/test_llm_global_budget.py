"""LLM 전역(전 사용자 합산) 일일 토큰 예산 (#325).

`llm_budget.check()` 는 이제 두 단계다: 전역 상한 먼저, 그다음 사용자별 상한.
이 파일이 못 박는 것:
- 전역 합산은 user_id 필터 없이 **모든 행**(다른 사용자 포함)을 더한다.
- 전역 상한이 사용자별 상한보다 먼저 걸린다 — 사용자별로는 여유가 있어도 막힌다.
- 전역 상한 0 은 무제한(다른 예산 가드들과 같은 관례).
- 사용자별 상한만 걸릴 때는 기존과 동일하게 `used`/`limit` 이 그 사용자 값이다.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.config import get_settings
from reaction_backend.db.models.llm_run import LlmRun
from reaction_backend.db.models.user import User
from reaction_backend.safety.llm_budget import BudgetExceeded, check
from reaction_backend.schemas.common import now_kst

pytestmark = pytest.mark.usefixtures("real_db_session")


@pytest.fixture(autouse=True)
def _pin_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_global_daily_token_budget", 1_000, raising=False)
    monkeypatch.setattr(settings, "llm_daily_token_budget", 10_000, raising=False)


def _run(user_id: uuid.UUID | None, *, tokens_in: int, tokens_out: int) -> LlmRun:
    return LlmRun(
        user_id=user_id,
        module="planning",
        model="gemini-3.5-flash-lite",
        prompt_id="planning/goal_decompose",
        prompt_version="1",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=1_000,
        cost_cents=0,
        cost_micro_usd=10,
        success=True,
        fell_back=False,
    )


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="전역 예산 테스트"))
    await session.flush()
    return user_id


async def _seed(session: AsyncSession, *rows: LlmRun) -> None:
    for r in rows:
        session.add(r)
    await session.flush()


async def test_global_cap_blocks_even_when_user_is_under_their_own_limit(
    real_db_session: AsyncSession,
) -> None:
    """다른 사용자들 합산이 전역 상한(1000)을 넘으면, 오늘 한 번도 안 쓴 사용자도 막힌다."""
    other_a = await _seed_user(real_db_session)
    other_b = await _seed_user(real_db_session)
    await _seed(
        real_db_session,
        _run(other_a, tokens_in=400, tokens_out=200),
        _run(other_b, tokens_in=300, tokens_out=200),
    )  # 전역 합산 1100 > 1000

    target = await _seed_user(real_db_session)
    with pytest.raises(BudgetExceeded) as exc:
        await check(real_db_session, user_id=target)
    assert exc.value.used == 1_100
    assert exc.value.limit == 1_000


async def test_global_cap_counts_all_users_not_just_caller(real_db_session: AsyncSession) -> None:
    """전역 합산은 user_id 필터가 없다 — 호출자 자신의 사용량만 보면 안 된다."""
    caller = await _seed_user(real_db_session)
    other = await _seed_user(real_db_session)
    await _seed(
        real_db_session,
        _run(caller, tokens_in=50, tokens_out=50),  # 호출자: 100
        _run(other, tokens_in=500, tokens_out=500),  # 남: 1000
    )  # 합산 1100 > 전역 1000 — 호출자 개인은 100밖에 안 썼어도 막힌다

    with pytest.raises(BudgetExceeded) as exc:
        await check(real_db_session, user_id=caller)
    assert exc.value.used == 1_100


async def test_under_global_cap_falls_through_to_per_user_check(
    real_db_session: AsyncSession,
) -> None:
    """전역 상한 밑이면 기존 사용자별 로직으로 넘어간다 — 회귀 없음."""
    user_id = await _seed_user(real_db_session)
    await _seed(real_db_session, _run(user_id, tokens_in=50, tokens_out=50))  # 전역 100, 개인 100

    status = await check(real_db_session, user_id=user_id)
    assert status.used == 100
    assert status.limit == 10_000  # 사용자별 상한


async def test_zero_global_limit_means_unlimited(real_db_session: AsyncSession) -> None:
    settings = get_settings()
    settings.llm_global_daily_token_budget = 0
    try:
        other = await _seed_user(real_db_session)
        await _seed(
            real_db_session, _run(other, tokens_in=5_000, tokens_out=5_000)
        )  # 전역만 보면 막힐 양

        target = await _seed_user(real_db_session)
        status = await check(real_db_session, user_id=target)  # 전역 가드 자체가 스킵됨
        assert status.used == 0
        assert status.limit == 10_000
    finally:
        settings.llm_global_daily_token_budget = 1_000


async def test_global_used_ignores_day_boundary_from_yesterday(
    real_db_session: AsyncSession,
) -> None:
    """어제치 토큰은 전역 합산에 안 들어간다 — KST 자정 경계."""
    from datetime import timedelta

    other = await _seed_user(real_db_session)
    yesterday_row = _run(other, tokens_in=5_000, tokens_out=5_000)
    yesterday_row.created_at = now_kst() - timedelta(days=1)
    await _seed(real_db_session, yesterday_row)

    target = await _seed_user(real_db_session)
    status = await check(real_db_session, user_id=target)
    assert status.used == 0
