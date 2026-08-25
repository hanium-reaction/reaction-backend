"""`ScheduledBlockRepo.list_stale_scheduled_before` 의 실 SQL 고정 — 밀린 일 회수.

`FakeScheduledBlockRepo` 가 라우트 테스트를 전부 받아내므로 실 repo 의 WHERE 는 스위트에서
한 번도 실행되지 않는다(`test_action_item_repo_sql.py` 가 확립한 문제의식). 이 쿼리는 조건
하나가 빠지거나 뒤집히면 **조용히 반대로 동작**한다:

- `block_status == 'scheduled'` 가 빠지면 → 이미 완료·취소한 블록까지 다시 배치 대상이 된다
- `source != 'user_edit'` 가 빠지면 → 사용자가 직접 옮긴 과거 블록을 재계획이 지운다(#113 위반)
- `start_at < before` 가 `>` 로 뒤집히면 → 미래 블록을 '밀린 일'로 잡아 `list_scheduled_between`
  과 **중복 후보**가 되고, 한 액션이 두 번 배치된다
- `ActionItem.archived_at IS NULL` 이 빠지면 → 보관(취소)한 카드가 되살아난다

컬럼명만 검사하면 위 넷 중 둘(부정·부등호 뒤집힘)이 그대로 생존하므로 **연산자까지 한
문자열로** 고정한다.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from reaction_backend.repositories.scheduled_block_repo import ScheduledBlockRepo
from reaction_backend.schemas.common import KST

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
BEFORE = datetime(2026, 7, 9, 12, 0, tzinfo=KST)


class _RecordingResult:
    def all(self) -> list[object]:
        return []


class _RecordingSession:
    """실행된 statement 를 붙잡아 두는 세션 — 실 repo 의 SQL 검사용."""

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


async def _run_list_stale() -> str:
    session = _RecordingSession()
    repo = ScheduledBlockRepo(session)  # type: ignore[arg-type]
    found = await repo.list_stale_scheduled_before(USER_ID, BEFORE)
    assert len(session.statements) == 1, "쿼리가 실행되지 않았다 — 검사가 공허하다"
    assert found == []  # all() 이 빈 목록이면 없음으로 읽는다 (뮤턴트 방지 겸)
    return _sql(session.statements[0])


async def test_stale_query_is_scoped_to_the_user() -> None:
    sql = await _run_list_stale()
    assert f"scheduled_blocks.user_id = '{USER_ID}'" in sql, sql


async def test_stale_query_only_sees_never_started_blocks() -> None:
    """`scheduled` 만 — 착수·완료·취소된 블록은 불변(AGENTS §2)."""
    sql = await _run_list_stale()
    assert "scheduled_blocks.block_status = 'scheduled'" in sql, sql


async def test_stale_query_preserves_user_moved_blocks() -> None:
    """`source != 'user_edit'` — 사용자가 직접 옮긴 블록은 재계획이 안 건드린다(#113).

    이 부정이 사라지거나 `=` 로 뒤집히면 정확히 반대로 동작한다.
    """
    sql = await _run_list_stale()
    assert "scheduled_blocks.source != 'user_edit'" in sql, sql


async def test_stale_query_looks_backward_not_forward() -> None:
    """`start_at < before` — 부등호가 뒤집히면 `list_scheduled_between` 과 후보가 겹쳐
    한 액션이 두 번 배치된다."""
    sql = await _run_list_stale()
    assert "scheduled_blocks.start_at < '2026-07-09 12:00:00+09:00'" in sql, sql


async def test_stale_query_excludes_archived_cards() -> None:
    """보관(취소)한 카드는 되살리지 않는다."""
    sql = await _run_list_stale()
    assert "action_items.archived_at IS NULL" in sql, sql


async def test_stale_query_joins_action_items() -> None:
    """블록만으로는 재계획 후보를 만들 수 없다 — 제목·분량이 ActionItem 에 있다."""
    sql = await _run_list_stale()
    assert "JOIN action_items" in sql, sql
