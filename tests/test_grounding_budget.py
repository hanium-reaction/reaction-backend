"""검색 그라운딩 예산 가드 (#259 §3).

**왜 토큰 예산으로 안 되는가**: 그라운딩은 토큰이 아니라 **요청 건수**로 과금되고(무료
5,000건/월, 초과분 $14/1,000건), 검색이 서버 쪽에서 일어나 입력 토큰이 17개로 잡힌다.
그래서 `tokens_in + tokens_out` 기반 일일 토큰 예산은 이 비용에 **완전히 눈이 멀어 있다** —
루프가 돌면 계량기는 0 인데 1,000건당 $14 가 나간다.

이 테스트가 못 박는 것:
- 그라운딩 호출이 **토큰 가드는 통과**한다 (그래서 별도 가드가 필요하다는 증명)
- 건수 한도를 넘으면 `GroundingBudgetExceeded`
- 한도 0 은 무제한 (토큰 예산과 같은 관례)
- 사용자별로 분리되고 KST 오늘 자만 센다
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.config import get_settings
from reaction_backend.db.models.llm_run import LlmRun
from reaction_backend.db.models.user import User
from reaction_backend.safety.llm_budget import (
    BudgetExceeded,
    GroundingBudgetExceeded,
    check,
    check_grounding,
)
from reaction_backend.schemas.common import now_kst

pytestmark = pytest.mark.usefixtures("real_db_session")


@pytest.fixture(autouse=True)
def _pin_llm_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    """이 파일의 테스트는 `get_settings()` 의 실제 한도값을 기준으로 seed 건수를 계산한다
    (예: `limit * 4`, `limit - 1`). 로컬 `.env`/환경변수가 0(무제한 관례, #259 §3)으로
    오버라이드해 두면 `limit`이 0이 돼 이 산술이 전부 깨지고 예산 가드가 트리거되지
    않는다 — 실제 시각에 따라 깨지는 harvest_slots 시한폭탄과 같은 종류의, 테스트가
    통제하지 않는 외부 상태(여기서는 환경설정)에 대한 암묵적 가정이다.

    테스트마다 알려진 양수 값으로 고정해 환경과 무관하게 만든다. `test_zero_limit_means_unlimited`
    는 이 위에서 grounding 한도를 0으로 다시 덮어써 자신의 케이스를 검증한다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_daily_grounding_budget", 5, raising=False)
    monkeypatch.setattr(settings, "llm_daily_token_budget", 200_000, raising=False)


def _grounding_run(user_id: uuid.UUID, *, requests: int, days_ago: int = 0) -> LlmRun:
    """그라운딩 호출 1건 — **토큰은 거의 0**이다(#259 §3 실측: in 17 / out 1,263)."""
    row = LlmRun(
        user_id=user_id,
        module="planning",
        model="gemini-flash-lite-latest",
        prompt_id="planning/materials_search",
        prompt_version="1",
        tokens_in=17,
        tokens_out=1_263,
        latency_ms=8_500,
        cost_cents=0,
        cost_micro_usd=3_200,
        grounding_requests=requests,
        success=True,
        fell_back=False,
    )
    if days_ago:
        row.created_at = now_kst() - timedelta(days=days_ago)
    return row


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    """`llm_runs.user_id` 에 FK 가 걸려 있어 실 users 행이 필요하다."""
    user_id = uuid.uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="그라운딩 예산 테스트"))
    await session.flush()
    return user_id


async def _seed(session: AsyncSession, *rows: LlmRun) -> None:
    for r in rows:
        session.add(r)
    await session.flush()


async def test_grounding_calls_pass_the_token_guard(real_db_session: AsyncSession) -> None:
    """**이 테스트가 이 기능의 존재 이유다.**

    그라운딩을 한도의 몇 배로 때려도 토큰 가드는 통과한다 — 검색이 토큰으로 안 잡히기
    때문이다. 이게 통과하는 한, 토큰 예산만으로는 그라운딩 비용을 막을 수 없다.
    """
    user_id = await _seed_user(real_db_session)
    limit = get_settings().llm_daily_grounding_budget
    await _seed(real_db_session, *[_grounding_run(user_id, requests=5) for _ in range(limit * 4)])

    # 토큰 가드: 통과한다 (호출당 1,280 토큰이라 20만 한도에 한참 못 미친다)
    status = await check(real_db_session, user_id=user_id)
    assert status.used < status.limit

    # 그라운딩 가드: 막는다
    with pytest.raises(GroundingBudgetExceeded):
        await check_grounding(real_db_session, user_id=user_id)


async def test_blocks_when_daily_requests_exceed_limit(real_db_session: AsyncSession) -> None:
    user_id = await _seed_user(real_db_session)
    limit = get_settings().llm_daily_grounding_budget
    await _seed(real_db_session, _grounding_run(user_id, requests=limit))

    with pytest.raises(GroundingBudgetExceeded) as exc:
        await check_grounding(real_db_session, user_id=user_id)
    assert exc.value.used == limit
    assert exc.value.limit == limit


async def test_allows_until_the_limit(real_db_session: AsyncSession) -> None:
    """경계값 — 한도 직전까지는 통과해야 한다. `>` 가 `>=` 로 바뀌면 여기서 죽는다."""
    user_id = await _seed_user(real_db_session)
    limit = get_settings().llm_daily_grounding_budget
    await _seed(real_db_session, _grounding_run(user_id, requests=limit - 1))

    status = await check_grounding(real_db_session, user_id=user_id, projected_requests=1)
    assert status.used == limit - 1
    assert status.remaining == 1


async def test_zero_limit_means_unlimited(
    real_db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """토큰 예산과 같은 관례 — 0 은 무제한(로컬 탐사에서 끄는 용도)."""
    user_id = await _seed_user(real_db_session)
    await _seed(real_db_session, _grounding_run(user_id, requests=10_000))

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_daily_grounding_budget", 0, raising=False)
    status = await check_grounding(real_db_session, user_id=user_id)
    assert status.limit == 0


async def test_other_users_and_other_days_do_not_count(real_db_session: AsyncSession) -> None:
    """사용자별·KST 오늘 자만 센다 — 남의 사용량이나 어제 것으로 막히면 안 된다."""
    me = await _seed_user(real_db_session)
    someone_else = await _seed_user(real_db_session)
    limit = get_settings().llm_daily_grounding_budget
    await _seed(
        real_db_session,
        _grounding_run(someone_else, requests=limit * 3),
        _grounding_run(me, requests=limit * 3, days_ago=1),
    )

    status = await check_grounding(real_db_session, user_id=me)
    assert status.used == 0


async def test_token_budget_still_blocks_ordinary_calls(real_db_session: AsyncSession) -> None:
    """반대 방향도 확인 — 그라운딩 가드가 생겼다고 토큰 가드가 느슨해지면 안 된다."""
    user_id = await _seed_user(real_db_session)
    heavy = LlmRun(
        user_id=user_id,
        module="planning",
        model="gemini-flash-latest",
        prompt_id="planning/goal_decompose",
        prompt_version="1",
        tokens_in=get_settings().llm_daily_token_budget,
        tokens_out=0,
        latency_ms=1,
        cost_cents=0,
        cost_micro_usd=0,
        success=True,
        fell_back=False,
    )
    await _seed(real_db_session, heavy)

    with pytest.raises(BudgetExceeded):
        await check(real_db_session, user_id=user_id, projected_tokens=1)
    # 그라운딩은 0 건이라 그쪽 가드는 통과
    assert (await check_grounding(real_db_session, user_id=user_id)).used == 0
