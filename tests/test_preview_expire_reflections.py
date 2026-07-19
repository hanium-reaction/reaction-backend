"""프리뷰 SELECT ↔ 실제 만료 UPDATE 의 WHERE 동일성 고정 (#20 DoD 2 · #24).

프리뷰(`scripts/preview_expire_reflections.py`)는 "켜면 무엇이 지워지나"를 답하는 도구다.
그 답이 맞으려면 프리뷰의 WHERE 가 `ExecutionRepo.expire_unreflected` 의 WHERE 와
**글자 단위로 같아야** 한다 — 만료 쿼리를 고치면서 프리뷰를 잊으면, 실측과 다른 수가
지워지는 최악의 배신이 된다. compile 된 SQL 로 두 WHERE 를 대조해 그 drift 를 CI 에서 잡는다.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from scripts.preview_expire_reflections import expire_candidates_stmt

from reaction_backend.schemas.common import KST

BEFORE = datetime(2026, 7, 14, 0, 0, tzinfo=KST)


def _literal_sql(stmt: object) -> str:
    """컴파일 + 공백 정규화 — SELECT 는 절 앞에 개행을 넣어 ` WHERE ` 매칭이 빗나간다."""
    from sqlalchemy.dialects import postgresql

    raw = str(
        stmt.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    return " ".join(raw.split())


async def test_preview_where_equals_expire_where() -> None:
    """프리뷰 SELECT 의 WHERE == 만료 UPDATE 의 WHERE (값까지 인라인해 대조)."""
    from reaction_backend.repositories.execution_repo import ExecutionRepo
    from tests.test_reflection_expiry import _RecordingSession

    session = _RecordingSession([uuid4()])
    repo = ExecutionRepo(session)  # type: ignore[arg-type]
    await repo.expire_unreflected(before=BEFORE, archived_at=BEFORE)
    update_where = (
        _literal_sql(session.statements[0]).partition(" WHERE ")[2].partition(" RETURNING ")[0]
    )

    select_where = _literal_sql(expire_candidates_stmt(BEFORE)).partition(" WHERE ")[2]
    assert select_where and update_where, "WHERE 추출이 비었다 — 대조 자체가 공허해진다"

    assert select_where == update_where, (
        "프리뷰와 실제 만료의 판정 조건이 갈라졌다 — 둘 중 하나를 고쳤으면 다른 쪽도 고칠 것. "
        f"\npreview: {select_where}\nexpire : {update_where}"
    )
