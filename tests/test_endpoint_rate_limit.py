"""비싼 엔드포인트 사용자별 일일 호출 한도 (#325).

이 테스트가 못 박는 것:
- **엔드포인트 실행 횟수**로 카운트한다 — `DISTINCT trace_id`, 행 수가 아니다 (#370).
  한 요청이 LLM 을 몇 번 부르든 1회로 센다.
- trace_id 가 없는 행(요청 밖 호출·과거 행)은 각각 1회 — 가드가 조용히 꺼지지 않게.
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


def _run(
    user_id: uuid.UUID,
    *,
    module: str = "planning",
    days_ago: int = 0,
    trace_id: str | None = None,
    prompt_id: str | None = "planning/goal_decompose",
    reason: str | None = None,
) -> LlmRun:
    """`llm_runs` 행 1개 = LLM 호출 1회. 같은 `trace_id` 를 준 행들은 **한 번의 실행**이다.

    trace_id 를 안 주면 None — 요청 밖(cron·스크립트)이나 이 수정 이전 행과 같은 상태이고,
    가드는 그런 행을 각각 1회로 센다.
    """
    row = LlmRun(
        user_id=user_id,
        module=module,
        model="gemini-3.5-flash-lite",
        prompt_id=prompt_id,
        prompt_version="1",
        tokens_in=100,
        tokens_out=200,
        latency_ms=1_000,
        cost_cents=0,
        cost_micro_usd=10,
        success=reason is None,
        fell_back=reason is not None,
        trace_id=trace_id,
        reason=reason,
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
    # 사용자를 탓하지 않는다 — '너무 많이 하셨어요' 대신 준비된 횟수를 다 썼다고 말한다(llm-4).
    assert err.message == "오늘 준비된 계획·만다라트 생성 횟수를 다 썼어요. 내일 다시 열려요."


def test_seconds_until_kst_midnight_at_end_of_day() -> None:
    almost_midnight = datetime(2026, 8, 25, 23, 59, 30, tzinfo=KST)
    assert seconds_until_kst_midnight(almost_midnight) == 30


def test_seconds_until_kst_midnight_just_after_midnight() -> None:
    just_after = datetime(2026, 8, 25, 0, 0, 1, tzinfo=KST)
    assert seconds_until_kst_midnight(just_after) == 24 * 3600 - 1


# ── #370: 계수 단위가 "행"이 아니라 "요청"이다 ──────────────────────────


async def test_one_request_many_llm_calls_counts_as_one(real_db_session: AsyncSession) -> None:
    """#370 의 핵심 — 한 요청이 LLM 을 3번 불러도 1회다.

    예전 구현은 행을 세서 이 케이스를 3회로 봤고, 그래서 상한 20 이 인터뷰 8턴이 됐다.
    한도 3 에 3콜짜리 요청 하나면 예전엔 즉시 차단, 지금은 통과해야 한다.
    """
    user_id = await _seed_user(real_db_session)
    trace = "req-single"
    await _seed(real_db_session, *[_run(user_id, trace_id=trace) for _ in range(3)])

    await check(real_db_session, user_id=user_id, module="planning")  # raise 없음


async def test_distinct_requests_accumulate(real_db_session: AsyncSession) -> None:
    """서로 다른 요청 3건은 3회 — 상한이 실제로 동작하는지."""
    user_id = await _seed_user(real_db_session)
    await _seed(
        real_db_session,
        *[_run(user_id, trace_id=f"req-{i}") for i in range(3) for _ in range(2)],
    )

    with pytest.raises(EndpointCallLimitExceeded) as exc:
        await check(real_db_session, user_id=user_id, module="planning")
    assert exc.value.used == 3  # 6행이지만 요청은 3건


async def test_null_trace_id_rows_count_individually(real_db_session: AsyncSession) -> None:
    """trace_id 가 없는 행은 각각 1회로 센다.

    `COUNT(DISTINCT trace_id)` 는 NULL 을 통째로 무시하므로, COALESCE 없이 짜면 요청 밖
    호출·과거 행이 전부 0 으로 세어져 **가드가 조용히 꺼진다**. 틀리더라도 빡빡한 쪽으로.
    """
    user_id = await _seed_user(real_db_session)
    await _seed(real_db_session, *[_run(user_id) for _ in range(3)])

    with pytest.raises(EndpointCallLimitExceeded) as exc:
        await check(real_db_session, user_id=user_id, module="planning")
    assert exc.value.used == 3


async def test_null_and_traced_rows_mix(real_db_session: AsyncSession) -> None:
    """과거 행(NULL 2건 = 2회) + 이후 요청 1건(3콜 = 1회) = 3회."""
    user_id = await _seed_user(real_db_session)
    await _seed(
        real_db_session,
        _run(user_id),
        _run(user_id),
        *[_run(user_id, trace_id="req-new") for _ in range(3)],
    )

    with pytest.raises(EndpointCallLimitExceeded) as exc:
        await check(real_db_session, user_id=user_id, module="planning")
    assert exc.value.used == 3


# ── #370: module 별 상한 ────────────────────────────────────────────────


def test_interview_gets_a_higher_limit_than_the_base() -> None:
    """인터뷰는 한 번 하는 데 요청이 ~20건 들어 base 상한으로는 완주가 안 된다.

    실제 완주 요청 수와의 관계는 `tests/test_endpoint_invocation_counting.py` 가 지킨다.
    여기서는 오버라이드가 살아 있는지(=우연히 지워지지 않았는지)만 못 박는다.
    """
    settings = get_settings()
    assert settings.endpoint_call_limit_for_module("interview") > (
        settings.endpoint_call_limit_for_module("planning")
    )


def test_other_modules_use_the_base_limit() -> None:
    settings = get_settings()
    base = settings.llm_endpoint_daily_call_limit
    for module in ("planning", "recovery", "brief", "inbox"):
        assert settings.endpoint_call_limit_for_module(module) == base


def test_zero_base_limit_disables_every_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """운영 스위치로 가드를 끄면 interview 오버라이드가 있어도 함께 꺼져야 한다.

    반만 꺼지면 "껐는데 인터뷰만 여전히 막힌다" 는 최악의 상태가 된다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_endpoint_daily_call_limit", 0, raising=False)
    assert settings.endpoint_call_limit_for_module("interview") == 0
    assert settings.endpoint_call_limit_for_module("planning") == 0


# ── llm-4: 상한을 안 거는 호출은 planning 계수에 들어가지 않는다 ─────────────

_UNGATED = (
    "planning/materials_search",
    "planning/plan_milestones",
    "planning/study_method",
    "planning/mandala_cells_branch",
)


async def test_refused_material_searches_do_not_block_plan_generation(
    real_db_session: AsyncSession,
) -> None:
    """실측: 계획을 한 번도 안 만든 사용자가 자료 검색 20번(15번은 그라운딩 예산에 걸려
    호출 없이 거절)으로 `/plans/generate` 429. 검색은 그라운딩 예산이 따로 막는다."""
    user_id = await _seed_user(real_db_session)
    await _seed(
        real_db_session,
        *[
            _run(
                user_id,
                trace_id=f"search-{i}",
                prompt_id="planning/materials_search",
                reason="grounding_budget" if i >= 2 else None,
            )
            for i in range(10)
        ],
    )

    await check(real_db_session, user_id=user_id, module="planning")  # raise 없음


@pytest.mark.parametrize("prompt_id", [*_UNGATED, "planning/study_method@v2"])
async def test_ungated_planning_prompts_are_not_counted(
    real_db_session: AsyncSession, prompt_id: str
) -> None:
    """마일스톤 초안·학습 방식·링 재생성은 상한을 안 거는 엔드포인트다 — 세지 않는다.
    `@v2` 는 프롬프트 렌더 실패 행이 호출부 id 를 그대로 남기는 모양."""
    user_id = await _seed_user(real_db_session)
    await _seed(
        real_db_session,
        *[_run(user_id, trace_id=f"u-{i}", prompt_id=prompt_id) for i in range(5)],
    )

    await check(real_db_session, user_id=user_id, module="planning")  # raise 없음


async def test_gated_requests_still_count_alongside_ungated_ones(
    real_db_session: AsyncSession,
) -> None:
    """분해 2건 + 무관한 호출 여럿 = 2회(통과), 분해 1건 더 = 3회(차단)."""
    user_id = await _seed_user(real_db_session)
    await _seed(
        real_db_session,
        _run(user_id, trace_id="gen-1"),
        _run(user_id, trace_id="gen-2"),
        *[_run(user_id, trace_id=f"other-{p}", prompt_id=p) for p in _UNGATED],
    )
    await check(real_db_session, user_id=user_id, module="planning")  # raise 없음

    await _seed(real_db_session, _run(user_id, trace_id="gen-3", prompt_id="planning/plan_quality"))
    with pytest.raises(EndpointCallLimitExceeded) as exc:
        await check(real_db_session, user_id=user_id, module="planning")
    assert exc.value.used == 3


async def test_gated_request_that_also_ran_an_ungated_prompt_counts_once(
    real_db_session: AsyncSession,
) -> None:
    """같은 요청 안에서 분해와 마일스톤을 같이 불렀어도 그 요청은 1회로 잡힌다."""
    user_id = await _seed_user(real_db_session)
    await _seed(
        real_db_session,
        *[
            row
            for i in range(3)
            for row in (
                _run(user_id, trace_id=f"gen-{i}"),
                _run(user_id, trace_id=f"gen-{i}", prompt_id="planning/plan_milestones"),
            )
        ],
    )

    with pytest.raises(EndpointCallLimitExceeded) as exc:
        await check(real_db_session, user_id=user_id, module="planning")
    assert exc.value.used == 3


async def test_rows_without_prompt_id_are_still_counted(real_db_session: AsyncSession) -> None:
    """출처를 모르는 행은 빡빡한 쪽으로 — 센다."""
    user_id = await _seed_user(real_db_session)
    await _seed(
        real_db_session, *[_run(user_id, trace_id=f"n-{i}", prompt_id=None) for i in range(3)]
    )

    with pytest.raises(EndpointCallLimitExceeded):
        await check(real_db_session, user_id=user_id, module="planning")


async def test_other_modules_keep_counting_every_prompt(real_db_session: AsyncSession) -> None:
    """제외 목록은 planning 에만 있다 — recovery 등은 종전대로 모든 행을 센다."""
    user_id = await _seed_user(real_db_session)
    await _seed(
        real_db_session,
        *[
            _run(user_id, module="recovery", trace_id=f"r-{i}", prompt_id="planning/study_method")
            for i in range(3)
        ],
    )

    with pytest.raises(EndpointCallLimitExceeded):
        await check(real_db_session, user_id=user_id, module="recovery")
