"""supersede 의 실 SQL 고정 — 교체 범위가 목표 단위인지 (`test_goal_repo_sql.py` 패턴).

`test_plan_approve_replace.py` 의 `_EntitySession` 은 **WHERE 를 평가하지 않는다**. 그래서
파이썬 술어(`_replaceable_action`)만 맞으면 SQL 에서 `goal_id` 조건이 통째로 빠져도 전 스위트가
초록이다. 그런데 SQL WHERE 는 그냥 성능 힌트가 아니다:

- `supersede_previous_plan` 의 SELECT 는 `FOR UPDATE` 다 → WHERE 가 넓으면 **남의 목표 카드까지
  행 잠금**을 잡아 동시 승인이 서로를 막는다.
- 파이썬 술어가 한 줄이라도 느슨해지는 순간(과거에 실제로 그랬다) SQL 이 마지막 방어선이다.

회귀 배경: 교체 키가 `target_date`(=계획 시작일) 뿐이던 시절, 같은 날 'MVP 만들기' 계획을
승인한 뒤 '토익' 계획을 승인하자 MVP 4주치(카드 12개)가 통째로 보관됐다.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

from reaction_backend.orchestrator.first_plan_adapter import (
    supersede_previous_plan,
    superseded_card_ids,
)

UID = uuid4()
GOAL = uuid4()
TARGET = date(2026, 7, 30)


class _RecordingResult:
    def scalars(self) -> _RecordingResult:
        return self

    def all(self) -> list[Any]:
        return []


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


async def test_supersede_select_scopes_to_goal() -> None:
    """카드 SELECT 가 user·**goal_id** 로 좁혀지고 FOR UPDATE 인지.

    target_date 는 **없어야 한다** (#222): 카드 날짜가 자기 블록 날짜를 따라가면서
    날짜 키로는 이전 계획의 뒷날짜 카드가 교체에서 빠져 재승인마다 누적된다.
    """
    sess = _RecordingSession()
    await supersede_previous_plan(sess, user_id=UID, goal_id=GOAL)  # type: ignore[arg-type]

    assert len(sess.statements) == 1  # 후보가 없으면 블록 SELECT 까지 가지 않는다
    sql = _sql(sess.statements[0])
    where = sql.split(" WHERE ", 1)[1]
    assert f"action_items.goal_id = '{GOAL}'" in where
    assert "target_date" not in where  # 교체 단위는 목표 전체 — 날짜 키 금지(#222)
    assert f"action_items.user_id = '{UID}'" in where
    assert "action_items.source IN ('goal')" in where  # 기본(승인 경로)은 계획 산출물만
    assert "action_items.status = 'planned'" in where
    assert "action_items.archived_at IS NULL" in where
    assert "FOR UPDATE" in sql  # 동시 [시작] 요청과 직렬화


async def test_supersede_select_widens_sources_for_completion() -> None:
    """완료 경로(`include_recovery=True`)만 회복 카드까지 SELECT 범위에 넣는다 (#367).

    SQL WHERE 가 안 넓어지면 파이썬 술어를 아무리 고쳐도 회복 카드는 **애초에 안 읽힌다** —
    `_RecordingSession` 은 WHERE 를 평가하지 않아 술어 테스트만으로는 이게 안 잡힌다.
    """
    sess = _RecordingSession()
    await supersede_previous_plan(  # type: ignore[arg-type]
        sess, user_id=UID, goal_id=GOAL, include_mandala=True, include_recovery=True
    )

    where = _sql(sess.statements[0]).split(" WHERE ", 1)[1]
    assert "action_items.source IN ('goal', 'recovery_downscope'" in where
    assert "'recovery_park'" in where  # v0.7.1 에서 늘어난 4번째 그룹까지
    assert f"action_items.goal_id = '{GOAL}'" in where  # 목표 스코프는 그대로


async def test_superseded_card_ids_select_scopes_to_goal() -> None:
    """busy 제외용 read-only SELECT 도 같은 범위 — 두 경로가 어긋나면 안 된다.

    generate 가 제외한 슬롯과 approve 가 지우는 슬롯이 다르면, 피하지 않은 자리를 지우거나
    (남의 계획 증발) 지우지 않을 자리를 피한다(빈 계획).
    """
    sess = _RecordingSession()
    await superseded_card_ids(sess, user_id=UID, goal_id=GOAL)  # type: ignore[arg-type]

    sql = _sql(sess.statements[0])
    where = sql.split(" WHERE ", 1)[1]
    assert f"action_items.goal_id = '{GOAL}'" in where
    assert "target_date" not in where  # supersede 와 같은 범위(#222)
    assert "FOR UPDATE" not in sql  # read-only — 잠금 걸지 않는다


async def test_superseded_card_ids_skips_query_without_goal() -> None:
    """goal_id=None 이면 쿼리조차 하지 않는다 — 제외할 대상이 없다(전부 회피)."""
    sess = _RecordingSession()
    ids = await superseded_card_ids(sess, user_id=UID, goal_id=None)  # type: ignore[arg-type]

    assert ids == set()
    assert sess.statements == []
