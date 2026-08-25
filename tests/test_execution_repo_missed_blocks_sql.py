"""`ExecutionRepo.list_active_blocks_for_actions` 실 SQL 고정 (근거 대장 §6.2 T1).

T1 미체크 배지의 재료 쿼리다 — 방향이 뒤집히면 조용히 틀린다:
- `block_status != 'cancelled'` 가 빠지면 취소된 블록도 미체크 후보에 낀다.
- `user_id` 가 빠지면 남의 블록으로 내 어젠다에 배지가 뜬다.

라우트 테스트는 `FakeExecutionRepo` 를 지나므로 이 WHERE 는 스위트에서 한 번도
실행되지 않는다(`test_execution_repo_history_sql.py` 에서 확립한 패턴 — recording
session 으로 실 SQL 문자열을 고정한다).
"""

from __future__ import annotations

from uuid import UUID

from reaction_backend.repositories.execution_repo import ExecutionRepo

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
ACTION_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ACTION_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


class _RecordingResult:
    def __iter__(self):  # noqa: ANN204
        return iter([])


class _RecordingSession:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, stmt: object) -> _RecordingResult:
        self.statements.append(stmt)
        return _RecordingResult()


def _sql(stmt: object) -> str:
    from sqlalchemy.dialects import postgresql

    raw = str(
        stmt.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    return " ".join(raw.split())


async def _run() -> str:
    session = _RecordingSession()
    repo = ExecutionRepo(session)  # type: ignore[arg-type]
    found = await repo.list_active_blocks_for_actions(USER_ID, [ACTION_A, ACTION_B])
    assert len(session.statements) == 1, "쿼리가 실행되지 않았다 — 검사가 공허하다"
    assert found == []
    return _sql(session.statements[0])


async def test_query_scopes_to_user_and_the_given_cards() -> None:
    sql = await _run()
    assert f"scheduled_blocks.user_id = '{USER_ID}'" in sql
    assert str(ACTION_A) in sql and str(ACTION_B) in sql
    assert "scheduled_blocks.action_item_id IN" in sql


async def test_query_excludes_cancelled_blocks() -> None:
    sql = await _run()
    assert "scheduled_blocks.block_status != 'cancelled'" in sql, (
        f"취소된 블록 제외가 빠졌다 — 미체크 후보에 낄 수 있다: {sql}"
    )


async def test_query_selects_only_the_three_needed_columns() -> None:
    sql = await _run()
    assert sql.startswith(
        "SELECT scheduled_blocks.action_item_id, "
        "scheduled_blocks.block_status, scheduled_blocks.start_at"
    ), sql


async def test_empty_input_does_not_query() -> None:
    """`IN ()` 는 PostgreSQL 문법 오류다 — 카드가 없는 날 어젠다가 500 이 된다."""
    session = _RecordingSession()
    repo = ExecutionRepo(session)  # type: ignore[arg-type]
    assert await repo.list_active_blocks_for_actions(USER_ID, []) == []
    assert session.statements == [], "빈 목록인데 쿼리를 날렸다"
