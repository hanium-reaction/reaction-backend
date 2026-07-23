"""PolicySnapshotRepo.get_active 의 실 SQL 고정 — fake 전면대체 대응 (#168).

`test_policy_snapshot.py` 는 `FakePolicySnapshotRepo` 를 태우므로 실 repo 의 WHERE/ORDER BY
는 **전 스위트에서 한 번도 실행되지 않는다** — `is_active` 필터를 빼거나 정렬을 ASC 로
뒤집어도(가장 오래된 스냅샷을 '현재'로 노출) CI 는 초록이다. 그래서 값까지 인라인한 실
SQL 문자열로 고정한다 (`test_review_repo_sql.py` 패턴).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4


class _RecordingResult:
    def scalars(self) -> _RecordingResult:
        return self

    def first(self) -> Any:
        return None


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


async def test_get_active_pins_active_filter_and_latest_first() -> None:
    """활성 스냅샷 조회가 (a) is_active 로 거르고 (b) version 내림차순으로 최신을 고른다.

    회귀: is_active 필터를 빼면 비활성(옛) 버전이 노출되고, ORDER BY 를 ASC 로 뒤집으면
    가장 오래된 스냅샷이 '현재 정책'으로 잘못 나간다 — 둘 다 fake 로는 못 잡는다.
    """
    from reaction_backend.repositories.policy_snapshot_repo import PolicySnapshotRepo

    session = _RecordingSession()
    repo = PolicySnapshotRepo(session)  # type: ignore[arg-type]
    await repo.get_active(uuid4())

    sql = _sql(session.statements[0])
    assert "policy_snapshots.user_id =" in sql
    assert "policy_snapshots.is_active IS true" in sql, f"활성 필터가 풀렸다: {sql}"
    assert "ORDER BY policy_snapshots.version DESC" in sql, f"최신 우선 정렬이 아니다: {sql}"
