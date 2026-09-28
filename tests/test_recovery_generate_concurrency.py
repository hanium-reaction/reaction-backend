"""같은 실행의 회복 생성은 한 번에 하나만 — 동시 generate 가 세트를 여러 벌 만들었다 (#481).

`generate` 는 "pending 이 있으면 그대로 반환" 이라는 멱등 계약을 갖지만, 그 계약이 **순차
호출에서만** 성립했다. 동시 요청은 pending 조회를 나란히 통과한 뒤 각자 LLM 을 부르고 각자
INSERT 한다. 실측(2026-09-16, main 358b231, 로컬 API + Docker Postgres):

    동시 2회 → proposal set 2벌 · LLM 2회 · rate 2회
    동시 3회 → 3벌 · 3회 · 3회        (500 도 제약 위반도 없다 — 막을 것이 없었다)

브라우저에서도 dev StrictMode 재진입으로 pending 6행이 만들어졌다. prod 빌드에 StrictMode
이중 호출이 없어도 더블탭·여러 탭·클라이언트/네트워크 재시도로 같은 교차가 난다 — FE
debounce 로 닫을 문제가 아니라 서버가 원자성을 보장해야 한다.

`tests/test_start_action_locking.py`(#368)와 같은 3층으로 못 박는다:

1. **SQL 핀** (DB 불필요) — 생성 경로의 조회에 `FOR UPDATE` 가 붙는가, 읽기 전용 조회에는
   **안 붙는가**. 불필요한 잠금은 그 자체로 결함이다.
2. **실 동시성** (실 Postgres, 커넥션 여러 개) — 그래서 세트가 정말 한 벌만 생기는가.
   ①만으로는 "FOR UPDATE 를 붙였다"만 알 뿐 "그래서 중복이 안 생긴다"는 모른다.
3. **배선 핀** — 라우터가 그 잠금 읽기를 실제로 쓰는가. "메서드는 있는데 호출부가 안 쓴다"
   로도 사고는 똑같이 재발한다.

②는 커밋이 필요해 `real_db_session`(롤백 격리)을 못 쓴다 — 전용 엔진으로 진짜 커밋하고
테스트가 스스로 치운다(`users` 삭제 → 전 경로 CASCADE).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.routes.recovery import generate_recovery_proposals
from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.execution_failure_tag import ExecutionFailureTag
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.repositories.action_item_repo import ActionItemRepo
from reaction_backend.repositories.recovery_repo import RecoveryRepo
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.recovery import (
    RecoveryGenerateRequest,
    RecoveryProposalLLM,
    RecoveryProposalsResponse,
)
from tests.conftest import DB_AVAILABLE

# ── ① SQL 핀 — DB 없이 컴파일된 문장만 본다 ──────────────────────────────


class _Result:
    def scalar_one_or_none(self) -> None:
        return None


class _RecordingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        self.statements.append(stmt)
        return _Result()


def _sql_of(session: _RecordingSession) -> str:
    return str(session.statements[0].compile(compile_kwargs={"literal_binds": False}))


async def test_generation_read_takes_a_row_lock() -> None:
    """생성 경로의 실행 조회는 `FOR UPDATE` 로 그 실행의 생성을 직렬화한다."""
    session = _RecordingSession()
    repo = RecoveryRepo(session)  # type: ignore[arg-type]

    await repo.get_execution_for_update(uuid.uuid4(), uuid.uuid4())

    assert "FOR UPDATE" in _sql_of(session), _sql_of(session)


async def test_read_only_execution_lookup_does_not_lock() -> None:
    """읽기 전용 조회에 잠금이 붙으면 결정·replan 경로가 서로를 막는다."""
    session = _RecordingSession()
    repo = RecoveryRepo(session)  # type: ignore[arg-type]

    await repo.get_execution(uuid.uuid4(), uuid.uuid4())

    assert "FOR UPDATE" not in _sql_of(session), _sql_of(session)


# ── ② 실 동시성 — 커넥션 여러 개로 교차 ──────────────────────────────────

pytestmark_db = pytest.mark.skipif(
    not DB_AVAILABLE, reason="DATABASE_URL not set — 실 동시성 테스트 skip"
)

# LLM 이 도는 동안 뒤 요청이 pending 조회에 닿게 하는 창. 잠금이 없으면 이 창에서 교차가
# 확정적으로 일어난다(수정 전 재현용). 잠금이 있으면 뒤 요청이 여기서 기다린다.
_LLM_SECONDS = 0.4


async def _sessions() -> AsyncIterator[Any]:
    """이 테스트 전용 엔진 — 커밋이 필요해 `real_db_session`(롤백 격리)을 못 쓴다."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from reaction_backend.config import get_settings
    from reaction_backend.db.session import normalize_async_url

    engine = create_async_engine(
        normalize_async_url(get_settings().database_url), poolclass=NullPool
    )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _seed(session: AsyncSession, *, executions: int = 1) -> tuple[User, list[uuid.UUID]]:
    """사용자 1명 + 실패 실행 N건 (각각 다른 카드). 커밋한다 — 다른 커넥션이 봐야 한다."""
    user = User(id=uuid.uuid4(), email=f"race481+{uuid.uuid4()}@test.local", name="race481")
    session.add(user)
    await session.flush()
    execution_ids: list[uuid.UUID] = []
    for i in range(executions):
        action = ActionItem(
            id=uuid.uuid4(),
            user_id=user.id,
            title=f"동시성 테스트 카드 {i}",
            target_date=now_kst().date(),
            estimated_minutes=30,
            category="study",
        )
        session.add(action)
        await session.flush()
        start = now_kst()
        block = ScheduledBlock(
            id=uuid.uuid4(),
            user_id=user.id,
            action_item_id=action.id,
            start_at=start,
            end_at=start + timedelta(minutes=30),
        )
        session.add(block)
        await session.flush()
        execution = ExecutionEvent(
            id=uuid.uuid4(),
            user_id=user.id,
            action_item_id=action.id,
            scheduled_block_id=block.id,
            plan_start_at=start,
            plan_end_at=start + timedelta(minutes=30),
            completion_status="failed",
        )
        session.add(execution)
        await session.flush()
        session.add(
            ExecutionFailureTag(
                id=uuid.uuid4(), execution_id=execution.id, tag_code="TIME_SHORTAGE"
            )
        )
        execution_ids.append(execution.id)
    await session.commit()
    return user, execution_ids


async def _cleanup(sm: Any, user_id: uuid.UUID) -> None:
    """이 테스트는 커밋하므로 스스로 치운다 — users 삭제로 전 경로 CASCADE."""
    async with sm() as s:
        await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
        await s.commit()


def _stub_llm(monkeypatch: pytest.MonkeyPatch, calls: list[str], *, fail: bool = False) -> None:
    from reaction_backend.llm import RunResult, aiClient

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        calls.append(kwargs["prompt_id"])
        await asyncio.sleep(_LLM_SECONDS)  # 실제 회복 LLM 은 수 초가 걸린다
        if fail:
            raise RuntimeError("LLM 경로 실패 — 트랜잭션이 롤백돼야 한다")
        return RunResult(
            value=RecoveryProposalLLM(
                strategy_code="x", if_clause="", then_clause="다듬은 문구", rationale=""
            ),
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="2",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)


async def _generate(sm: Any, user: User, execution_id: uuid.UUID) -> RecoveryProposalsResponse:
    """라우터 1회 호출 — 요청마다 **독립 세션/트랜잭션**이라 실제 교차가 난다."""
    async with sm() as session:
        response = await generate_recovery_proposals(
            RecoveryGenerateRequest(execution_id=f"exec_{execution_id}"),
            user,
            RecoveryRepo(session),
            ActionItemRepo(session),
            session,
        )
        return response


async def _attempt_ids(sm: Any, execution_id: uuid.UUID) -> list[str]:
    async with sm() as s:
        rows = await s.execute(
            text(
                "SELECT id::text FROM recovery_attempts "
                "WHERE execution_id = :i ORDER BY recovery_strategy_type, id"
            ),
            {"i": execution_id},
        )
        return [r[0] for r in rows]


async def _llm_run_rows(sm: Any, user_id: uuid.UUID) -> int:
    async with sm() as s:
        return int(
            (
                await s.execute(
                    text(
                        "SELECT count(*) FROM llm_runs WHERE user_id = :i AND module = 'recovery'"
                    ),
                    {"i": user_id},
                )
            ).scalar_one()
        )


@pytestmark_db
@pytest.mark.parametrize("concurrent", [2, 3])
async def test_concurrent_generate_creates_exactly_one_proposal_set(
    monkeypatch: pytest.MonkeyPatch, concurrent: int
) -> None:
    """Case 1·2 — 동시 2회/3회가 세트를 정확히 한 벌만 만들고, 모두 같은 카드를 받는다."""
    agen = _sessions()
    sm = await anext(agen)
    calls: list[str] = []
    _stub_llm(monkeypatch, calls)
    try:
        async with sm() as s:
            user, (execution_id,) = await _seed(s)

        responses = await asyncio.wait_for(
            asyncio.gather(*(_generate(sm, user, execution_id) for _ in range(concurrent))),
            timeout=60,
        )

        rows = await _attempt_ids(sm, execution_id)
        card_ids = [sorted(c.attempt_id for c in r.cards) for r in responses]
        await _cleanup(sm, user.id)

        assert len(calls) == 1, f"LLM 이 {len(calls)}회 불렸다 — 생성이 직렬화되지 않았다"
        assert len(rows) == len(responses[0].cards), (
            f"pending 행 {len(rows)}개 — 세트가 여러 벌 생겼다 (카드 {len(responses[0].cards)}장)"
        )
        assert all(ids == card_ids[0] for ids in card_ids), (
            f"동시 요청이 서로 다른 세트를 받았다: {card_ids}"
        )
        assert sorted(f"rec_{r}" for r in rows) == card_ids[0]
    finally:
        await agen.aclose()


@pytestmark_db
async def test_sequential_generate_still_reuses_the_pending_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 3 — 순차 재호출의 멱등은 그대로다(잠금이 새 세트를 만들게 하지 않는다)."""
    agen = _sessions()
    sm = await anext(agen)
    calls: list[str] = []
    _stub_llm(monkeypatch, calls)
    try:
        async with sm() as s:
            user, (execution_id,) = await _seed(s)

        first = await _generate(sm, user, execution_id)
        second = await _generate(sm, user, execution_id)

        rows = await _attempt_ids(sm, execution_id)
        await _cleanup(sm, user.id)

        assert [c.attempt_id for c in first.cards] == [c.attempt_id for c in second.cards]
        assert len(rows) == len(first.cards)
        assert len(calls) == 1
    finally:
        await agen.aclose()


@pytestmark_db
async def test_rolled_back_generation_releases_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case 4 — 첫 생성이 실패하면 잠금이 풀리고 다음 요청이 새 세트를 만든다(교착 없음)."""
    agen = _sessions()
    sm = await anext(agen)
    failing: list[str] = []
    _stub_llm(monkeypatch, failing, fail=True)
    try:
        async with sm() as s:
            user, (execution_id,) = await _seed(s)

        with pytest.raises(RuntimeError):
            await _generate(sm, user, execution_id)

        assert await _attempt_ids(sm, execution_id) == [], "롤백됐는데 카드가 남았다"

        succeeding: list[str] = []
        _stub_llm(monkeypatch, succeeding)
        response = await asyncio.wait_for(_generate(sm, user, execution_id), timeout=30)

        rows = await _attempt_ids(sm, execution_id)
        await _cleanup(sm, user.id)

        assert len(response.cards) >= 2
        assert len(rows) == len(response.cards)
        assert len(succeeding) == 1
    finally:
        await agen.aclose()


@pytestmark_db
async def test_different_executions_are_not_serialized_against_each_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 5 — 잠금 단위는 실행이다. 같은 사용자의 다른 실패까지 줄 세우면 안 된다.

    직렬화되면 두 생성이 LLM 시간만큼 **차례로** 걸린다. 겹쳐 돌면 한 번 분에 가깝다.
    """
    agen = _sessions()
    sm = await anext(agen)
    calls: list[str] = []
    _stub_llm(monkeypatch, calls)
    try:
        async with sm() as s:
            user, (exec_a, exec_b) = await _seed(s, executions=2)

        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(
            asyncio.gather(_generate(sm, user, exec_a), _generate(sm, user, exec_b)), timeout=60
        )
        elapsed = asyncio.get_running_loop().time() - started

        rows_a = await _attempt_ids(sm, exec_a)
        rows_b = await _attempt_ids(sm, exec_b)
        llm_rows = await _llm_run_rows(sm, user.id)
        await _cleanup(sm, user.id)

        assert len(calls) == 2, "실행이 둘이면 생성도 둘이다"
        assert rows_a and rows_b
        assert llm_rows == 0, "stub 이라 llm_runs 는 안 쌓인다 — rate 소비 비교의 기준선"
        assert elapsed < _LLM_SECONDS * 2, (
            f"두 실행의 생성이 직렬화됐다 ({elapsed:.2f}s ≥ {_LLM_SECONDS * 2}s) — "
            "잠금 단위가 실행보다 넓다"
        )
    finally:
        await agen.aclose()


# ── ③ 배선 핀 — 라우터가 잠금 읽기를 쓰는가 ─────────────────────────────


def test_generate_route_uses_the_locking_read(
    client: Any, fake_recovery_repo: Any, fake_action_item_repo: Any
) -> None:
    """라우터가 잠금 없는 `get_execution` 으로 되돌아가면 여기서 잡힌다.

    ①②는 repo 메서드의 성질과 실제 직렬화를 본다 — 라우터가 그 메서드를 **쓰는지**는 별개다.
    """
    from tests.conftest import DEMO_USER_UUID

    action = ActionItem()
    action.id = uuid.uuid4()
    action.user_id = DEMO_USER_UUID
    action.title = "잠금 배선 확인"
    action.target_date = now_kst().date()
    action.category = "study"
    action.source = "manual"
    action.status = "failed"
    action.priority = 3
    action.estimated_minutes = 30
    action.why_now = None
    action.first_step = None
    action.goal_id = None
    action.archived_at = None
    fake_action_item_repo.seed(action)
    execution = fake_recovery_repo.register_execution(
        user_id=DEMO_USER_UUID, action_item_id=action.id, failure_tags=["TIME_SHORTAGE"]
    )

    response = client.post(
        "/recovery/proposals/generate", json={"executionId": f"exec_{execution.id}"}
    )

    assert response.status_code == 201, response.text
    assert execution.id in fake_recovery_repo.locking_reads, (
        "generate 가 잠금 읽기를 안 썼다 — 동시 요청이 다시 세트를 여러 벌 만든다 (#481)"
    )


def test_decision_route_does_not_take_the_generation_lock(
    client: Any, fake_recovery_repo: Any, fake_action_item_repo: Any
) -> None:
    """결정 경로까지 생성 잠금을 잡으면 생성 중인 요청과 서로를 막는다 — 읽기 그대로 둔다."""
    from tests.conftest import DEMO_USER_UUID

    action = ActionItem()
    action.id = uuid.uuid4()
    action.user_id = DEMO_USER_UUID
    action.title = "결정 경로 확인"
    action.target_date = now_kst().date()
    action.category = "study"
    action.source = "manual"
    action.status = "failed"
    action.priority = 3
    action.estimated_minutes = 30
    action.why_now = None
    action.first_step = None
    action.goal_id = None
    action.archived_at = None
    fake_action_item_repo.seed(action)
    execution = fake_recovery_repo.register_execution(
        user_id=DEMO_USER_UUID, action_item_id=action.id, failure_tags=["TIME_SHORTAGE"]
    )
    client.post("/recovery/proposals/generate", json={"executionId": f"exec_{execution.id}"})
    fake_recovery_repo.locking_reads.clear()

    response = client.post(
        "/recovery/decisions",
        json={"executionId": f"exec_{execution.id}", "decision": "skipped"},
        headers={"Idempotency-Key": f"test-{uuid.uuid4()}"},
    )

    assert response.status_code == 200, response.text
    assert fake_recovery_repo.locking_reads == []
