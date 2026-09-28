"""회복 결정·재배치 승인은 겹쳐 들어와도 한 번만 적힌다 — 카드·블록이 두 벌 생겼다.

#481 이 **생성**의 동시성을 닫았다. 그 뒤의 두 쓰기 경로도 같은 `check → write` 모양인데
경계가 없었다:

- `POST /recovery/decisions` — pending 을 잠금 없이 읽고 채택·거절·스킵을 적는다. 겹친
  수락 두 번이면 회복 ActionItem 이 **두 개** 생기고(`resulting_action_item_id` 는 뒤의 것만
  가리켜 앞의 것은 고아 카드로 오늘 화면에 남는다), 수락과 「나중에」가 겹치면 ORM 이 바뀐
  컬럼만 UPDATE 해서 **skipped 인데 회복 카드가 달린** 행이 커밋된다.
- `POST /replan/{executionId}/approve` — "블록이 이미 있나" 를 잠금 없이 보고 INSERT 한다.
  겹치면 같은 회복 카드에 블록이 두 개다(`create_block` 은 겹침 검사를 안 한다).

Idempotency-Key 미들웨어는 **끝난** 응답만 재생하므로 더블탭·재시도처럼 겹친 요청은 둘 다
라우터까지 온다 — 서버가 행 잠금으로 닫아야 한다.

`tests/test_recovery_generate_concurrency.py`(#481)와 같은 3층으로 못 박는다:

1. **SQL 핀** (DB 불필요) — 결정 조회에 `FOR UPDATE OF recovery_attempts` 가 붙는가, 읽기
   전용 조회에는 안 붙는가.
2. **실 동시성** (실 Postgres, 커넥션 여러 개) — 그래서 정말 한 번만 적히는가. 읽은 직후에
   창을 벌려(`_WINDOW`) 잠금이 없으면 교차가 확정적으로 나게 한다.
3. **배선 핀** — 라우터가 그 잠금 읽기를 실제로 쓰는가.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.routes.recovery import approve_replan, decide_recovery
from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.recovery_attempt import RecoveryAttempt
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.repositories.action_item_repo import ActionItemRepo
from reaction_backend.repositories.profile_repo import ProfileRepo
from reaction_backend.repositories.recovery_repo import RecoveryRepo
from reaction_backend.repositories.scheduled_block_repo import ScheduledBlockRepo
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.recovery import RecoveryDecisionRequest
from tests.conftest import DB_AVAILABLE, FakeActionItemRepo, FakeRecoveryRepo
from tests.test_recovery import _accept_group, _approve_replan, _decide, _seed_failed_execution

# ── ① SQL 핀 — DB 없이 컴파일된 문장만 본다 ──────────────────────────────


class _Scalars:
    def all(self) -> list[Any]:
        return []


class _Result:
    def scalars(self) -> _Scalars:
        return _Scalars()


class _RecordingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        self.statements.append(stmt)
        return _Result()


def _pg_sql(session: _RecordingSession) -> str:
    return str(session.statements[0].compile(dialect=postgresql.dialect()))


async def test_decision_read_locks_only_the_attempt_rows() -> None:
    """결정 조회는 카드 행만 잠근다 — 카탈로그는 외부 조인의 nullable 측이라 잠그면 Postgres 가
    거부하고(`FOR UPDATE cannot be applied to the nullable side of an outer join`), 잠글 이유도
    없다."""
    session = _RecordingSession()
    repo = RecoveryRepo(session)  # type: ignore[arg-type]

    await repo.list_attempts_for_update(uuid.uuid4(), uuid.uuid4())

    assert "FOR UPDATE OF recovery_attempts" in _pg_sql(session), _pg_sql(session)


async def test_read_only_attempt_listing_does_not_lock() -> None:
    """조회·생성·replan 프리뷰가 쓰는 목록에 잠금이 붙으면 서로를 막는다."""
    session = _RecordingSession()
    repo = RecoveryRepo(session)  # type: ignore[arg-type]

    await repo.list_attempts(uuid.uuid4(), uuid.uuid4())

    assert "FOR UPDATE" not in _pg_sql(session), _pg_sql(session)


# ── ② 실 동시성 — 커넥션 여러 개로 교차 ──────────────────────────────────

pytestmark_db = pytest.mark.skipif(
    not DB_AVAILABLE, reason="DATABASE_URL not set — 실 동시성 테스트 skip"
)

# 판정 읽기 직후 벌리는 창. 잠금이 없으면 두 요청이 이 창에서 같은 상태를 보고 각자 쓴다
# (수정 전 재현용). 잠금이 있으면 뒤 요청은 읽기 자체에서 앞 요청의 커밋을 기다린다.
_WINDOW = 0.4


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


async def _seed(session: AsyncSession) -> tuple[User, uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    """사용자 + 실패 실행 1건 + pending 카드 3장(DOWNSCOPE·CARRY_OVER·PARK). 커밋한다.

    반환: (user, execution_id, 원본 action_id, {option_group: attempt_id})
    """
    user = User(id=uuid.uuid4(), email=f"race-decide+{uuid.uuid4()}@test.local", name="race")
    session.add(user)
    await session.flush()
    action = ActionItem(
        id=uuid.uuid4(),
        user_id=user.id,
        title="동시 결정 테스트 카드",
        target_date=now_kst().date(),
        estimated_minutes=30,
        category="study",
    )
    session.add(action)
    await session.flush()
    start = now_kst() - timedelta(hours=2)
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
    attempts: dict[str, uuid.UUID] = {}
    for group, strategy in (
        ("DOWNSCOPE", "DOWNSCOPE_DEFAULT"),
        ("CARRY_OVER", "CARRYOVER_DEFAULT"),
        ("PARK", "PARK_DEFAULT"),
    ):
        attempt = RecoveryAttempt(
            id=uuid.uuid4(),
            user_id=user.id,
            execution_id=execution.id,
            recovery_option_group=group,
            recovery_strategy_type=strategy,
            suggested_action_text=f"{group} 제안",
            user_decision="pending",
        )
        session.add(attempt)
        attempts[group] = attempt.id
    await session.commit()
    # 라우터가 세션 밖에서 user 컬럼(활동 시간대 등)을 읽는다 — 전부 로드해 둔다.
    await session.refresh(user)
    return user, execution.id, action.id, attempts


async def _cleanup(sm: Any, user_id: uuid.UUID) -> None:
    """이 테스트는 커밋하므로 스스로 치운다 — users 삭제로 전 경로 CASCADE."""
    async with sm() as s:
        await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
        await s.commit()


def _widen_after_read(monkeypatch: pytest.MonkeyPatch, cls: type, *names: str) -> None:
    """판정 읽기가 **끝난 뒤** `_WINDOW` 만큼 쉰다 — 잠금이 없으면 이 창에서 교차가 확정된다."""
    for name in names:
        original: Callable[..., Awaitable[Any]] = getattr(cls, name)

        async def slow(self: Any, *args: Any, _original: Any = original, **kwargs: Any) -> Any:
            result = await _original(self, *args, **kwargs)
            await asyncio.sleep(_WINDOW)
            return result

        monkeypatch.setattr(cls, name, slow)


async def _decide_once(sm: Any, user: User, execution_id: uuid.UUID, body: dict[str, Any]) -> Any:
    """라우터 1회 호출 — 요청마다 **독립 세션/트랜잭션**이라 실제 교차가 난다."""
    async with sm() as session:
        return await decide_recovery(
            RecoveryDecisionRequest(execution_id=f"exec_{execution_id}", **body),
            user,
            RecoveryRepo(session),
            ActionItemRepo(session),
            session,
        )


async def _approve_once(sm: Any, user: User, execution_id: uuid.UUID) -> Any:
    async with sm() as session:
        return await approve_replan(
            f"exec_{execution_id}",
            user,
            RecoveryRepo(session),
            ActionItemRepo(session),
            ScheduledBlockRepo(session),
            ProfileRepo(session),
            session,
        )


def _split(results: list[Any]) -> tuple[list[Any], list[BaseException]]:
    ok = [r for r in results if not isinstance(r, BaseException)]
    errors = [r for r in results if isinstance(r, BaseException)]
    return ok, errors


def _already_decided(error: BaseException) -> bool:
    return isinstance(error, ApiError) and error.code == ErrorCode.RECOVERY_ALREADY_DECIDED


async def _attempt_rows(sm: Any, execution_id: uuid.UUID) -> list[tuple[str, str | None]]:
    async with sm() as s:
        rows = await s.execute(
            text(
                "SELECT user_decision::text, resulting_action_item_id::text "
                "FROM recovery_attempts WHERE execution_id = :i ORDER BY id"
            ),
            {"i": execution_id},
        )
        return [(r[0], r[1]) for r in rows]


async def _recovery_actions(sm: Any, original_id: uuid.UUID) -> list[str]:
    async with sm() as s:
        rows = await s.execute(
            text("SELECT id::text FROM action_items WHERE parent_action_item_id = :i"),
            {"i": original_id},
        )
        return [r[0] for r in rows]


@pytestmark_db
async def test_concurrent_accepts_create_one_recovery_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """같은 카드 수락이 겹쳐도 회복 ActionItem 은 하나 — 뒤 요청은 기존 계약대로 409."""
    agen = _sessions()
    sm = await anext(agen)
    _widen_after_read(monkeypatch, RecoveryRepo, "list_attempts", "list_attempts_for_update")
    try:
        async with sm() as s:
            user, execution_id, original_id, attempts = await _seed(s)

        body = {"decision": "accepted", "accepted_attempt_id": f"rec_{attempts['DOWNSCOPE']}"}
        results = await asyncio.wait_for(
            asyncio.gather(
                _decide_once(sm, user, execution_id, body),
                _decide_once(sm, user, execution_id, body),
                return_exceptions=True,
            ),
            timeout=60,
        )
        created = await _recovery_actions(sm, original_id)
        rows = await _attempt_rows(sm, execution_id)
        await _cleanup(sm, user.id)

        ok, errors = _split(results)
        assert len(created) == 1, f"회복 카드가 {len(created)}개 생겼다 — 결정이 직렬화되지 않았다"
        assert len(ok) == 1 and len(errors) == 1, f"성공 {len(ok)}·실패 {errors}"
        assert _already_decided(errors[0]), repr(errors[0])
        adopted = [r for r in rows if r[0] == "accepted"]
        assert len(adopted) == 1 and adopted[0][1] == created[0], rows
        assert sorted(r[0] for r in rows) == ["accepted", "rejected", "rejected"], rows
    finally:
        await agen.aclose()


@pytestmark_db
async def test_accept_racing_skip_leaves_one_consistent_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """수락과 「나중에」가 겹치면 **둘 중 하나만** 적힌다 — 섞인 행이 남지 않는다."""
    agen = _sessions()
    sm = await anext(agen)
    _widen_after_read(monkeypatch, RecoveryRepo, "list_attempts", "list_attempts_for_update")
    try:
        async with sm() as s:
            user, execution_id, original_id, attempts = await _seed(s)

        results = await asyncio.wait_for(
            asyncio.gather(
                _decide_once(
                    sm,
                    user,
                    execution_id,
                    {"decision": "accepted", "accepted_attempt_id": f"rec_{attempts['DOWNSCOPE']}"},
                ),
                _decide_once(sm, user, execution_id, {"decision": "skipped"}),
                return_exceptions=True,
            ),
            timeout=60,
        )
        created = await _recovery_actions(sm, original_id)
        rows = await _attempt_rows(sm, execution_id)
        await _cleanup(sm, user.id)

        ok, errors = _split(results)
        assert len(ok) == 1 and len(errors) == 1, f"성공 {len(ok)}·실패 {errors} — 둘 다 적혔다"
        assert _already_decided(errors[0]), repr(errors[0])
        decisions = sorted(r[0] for r in rows)
        if decisions == ["skipped", "skipped", "skipped"]:
            assert created == [] and all(r[1] is None for r in rows), (rows, created)
        else:
            assert decisions == ["accepted", "rejected", "rejected"], rows
            assert len(created) == 1, created
            assert [r[1] for r in rows if r[0] == "accepted"] == created, rows
    finally:
        await agen.aclose()


@pytestmark_db
async def test_concurrent_replan_approvals_place_one_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """재배치 승인이 겹쳐도 블록은 하나 — 뒤 요청은 그 블록을 그대로 받는다(멱등, 새 에러 없음)."""
    agen = _sessions()
    sm = await anext(agen)
    try:
        async with sm() as s:
            user, execution_id, _original_id, attempts = await _seed(s)
        accepted = await _decide_once(
            sm,
            user,
            execution_id,
            {"decision": "accepted", "accepted_attempt_id": f"rec_{attempts['DOWNSCOPE']}"},
        )
        recovery_action_id = uuid.UUID(accepted.resulting_action_item_id.removeprefix("action_"))

        _widen_after_read(monkeypatch, ScheduledBlockRepo, "list_by_action_item")
        results = await asyncio.wait_for(
            asyncio.gather(
                _approve_once(sm, user, execution_id),
                _approve_once(sm, user, execution_id),
                return_exceptions=True,
            ),
            timeout=60,
        )
        async with sm() as s:
            blocks = [
                r[0]
                for r in await s.execute(
                    text(
                        "SELECT id::text FROM scheduled_blocks "
                        "WHERE action_item_id = :i AND block_status <> 'cancelled'"
                    ),
                    {"i": recovery_action_id},
                )
            ]
        await _cleanup(sm, user.id)

        ok, errors = _split(results)
        assert errors == [], errors
        assert len(blocks) == 1, f"블록이 {len(blocks)}개 생겼다 — 재배치 승인이 직렬화되지 않았다"
        assert {r.scheduled_block_id for r in ok} == {f"block_{blocks[0]}"}, ok
    finally:
        await agen.aclose()


# ── ③ 배선 핀 — 라우터가 잠금 읽기를 쓰는가 ─────────────────────────────


def test_decision_route_uses_the_attempt_locking_read(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """결정이 잠금 없는 `list_attempts` 로 되돌아가면 여기서 잡힌다."""
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    client.post("/recovery/proposals/generate", json={"executionId": exec_id})

    response = _decide(client, {"executionId": exec_id, "decision": "skipped"})

    assert response.status_code == 200, response.text
    assert uuid.UUID(exec_id.removeprefix("exec_")) in fake_recovery_repo.attempt_locking_reads


def test_replan_approve_locks_the_recovery_card_but_preview_does_not(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """approve 는 회복 카드를 잠그고 블록 유무를 판정한다. GET 프리뷰는 읽기 그대로다."""
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    accepted = _accept_group(client, exec_id, "DOWNSCOPE")
    recovery_action_id = uuid.UUID(accepted["resultingActionItemId"].removeprefix("action_"))
    fake_action_item_repo.locking_reads.clear()

    assert client.get(f"/replan/{exec_id}").status_code == 200
    assert fake_action_item_repo.locking_reads == [], "프리뷰가 회복 카드를 잠갔다"

    response = _approve_replan(client, exec_id)

    assert response.status_code == 200, response.text
    assert fake_action_item_repo.locking_reads == [recovery_action_id]
