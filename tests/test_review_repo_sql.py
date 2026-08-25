"""주간 리뷰 집계 SQL 이 '수정 수락'을 회복으로 세는지 — 실 SQL 문자열로 고정 (#20 DoD 7).

왜 이 파일이 필요한가:
`FakeReviewRepo`(conftest)가 `collect_execution_stats`/`collect_recovery_stats` 의 **결론을
직접 주입**받아 돌려주므로, 실 `ReviewRepo` 의 WHERE 절은 **전 스위트에서 한 번도 실행되지
않는다**. 즉 `user_decision == "accepted"` 하드코딩을 그대로 두고 'edited' 를 추가하면,
편집으로 회복한 사용자가 resilience_rate 분자와 average_recovery_minutes 에서 **조용히
빠지는데 CI 는 초록**이다. AGENTS.md §2 가 지키려는 바로 그 지표가 오염된다.

그래서 fake 를 우회해 실 repo 가 내보내는 SQL 을 값까지 인라인해 검사한다
(만료 cron 에서 확립한 `literal_binds` 패턴과 동일).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.execution_failure_tag import ExecutionFailureTag
from reaction_backend.db.models.recovery_attempt import ADOPTED_DECISION_VALUES
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.schemas.common import KST
from tests.conftest import DB_AVAILABLE

START = datetime(2026, 7, 13, tzinfo=KST)
END = START + timedelta(days=7)


class _RecordingResult:
    def all(self) -> list[Any]:
        return []

    def scalars(self) -> _RecordingResult:
        return self

    def __iter__(self) -> Any:
        return iter([])


class _RecordingSession:
    """실행된 statement 를 붙잡아 두는 세션 — 실 repo 의 SQL 을 검사하기 위한 것."""

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


async def test_resilience_numerator_counts_edited_as_recovered() -> None:
    """resilience 분자 SQL 이 accepted **와 edited** 를 모두 센다.

    회귀: `== 'accepted'` 로 두면 문구를 고쳐 수락한 사용자가 회복에서 빠진다.
    """
    from reaction_backend.repositories.review_repo import ReviewRepo

    session = _RecordingSession()
    repo = ReviewRepo(session)  # type: ignore[arg-type]
    await repo._recovered_execution_ids(uuid4(), [uuid4()])

    sql = _sql(session.statements[0])
    assert "recovery_attempts.user_decision IN ('accepted', 'edited')" in sql, (
        f"편집 수락이 resilience 분자에서 빠진다: {sql}"
    )


async def test_average_recovery_minutes_counts_edited() -> None:
    """average_recovery_minutes 집계도 edited 를 포함한다."""
    from reaction_backend.repositories.review_repo import ReviewRepo

    session = _RecordingSession()
    repo = ReviewRepo(session)  # type: ignore[arg-type]
    await repo.collect_recovery_stats(uuid4(), START, END)

    sql = _sql(session.statements[0])
    assert "recovery_attempts.user_decision IN ('accepted', 'edited')" in sql, (
        f"편집 수락이 평균 회복 시간에서 빠진다: {sql}"
    )


def test_adopted_values_cover_every_decision_that_creates_a_card() -> None:
    """'카드를 채택한 결정' 집합이 enum 전체와 어긋나지 않는다.

    미래에 `USER_DECISION_VALUES` 에 값이 늘면, 그것이 채택인지 아닌지 분류하기 전까지
    이 테스트가 실패한다 — 새 값이 지표에서 조용히 누락되는 것을 막는다.
    """
    from reaction_backend.db.models.recovery_attempt import USER_DECISION_VALUES

    not_adopted = {"pending", "rejected", "skipped"}
    assert set(USER_DECISION_VALUES) == set(ADOPTED_DECISION_VALUES) | not_adopted


# ═══════════ get_top_failure_contexts — 실 Postgres (#301, SQL#4 파생) ═══════════
#
# 위 테스트들과 달리 여기는 `_RecordingSession` 이 아니라 실 DB 를 쓴다 — LIMIT/윈도우
# 함수의 상호작용(반환된 3건의 share 합이 1.0 이 아님)과 `failure_reason_tags` 조인이
# 실제로 맞물리는지는 SQL 문자열만 봐서는 알 수 없어서다. `tests/test_recovery_evidence_
# sql.py` 가 검증한 근거 대장 SQL#4 원문 자체는 건드리지 않는다(그 파일의 핀 의미는
# "문서의 SQL 을 한 글자도 안 고친다") — 여기서는 label_ko 조인이 추가된 **프로덕션
# 버전**(`review_repo._TOP_FAILURE_CONTEXTS_SQL`)을 별도로 검증한다.


async def _seed_user_real(session: AsyncSession) -> UUID:
    user_id = uuid4()
    session.add(
        User(id=user_id, email=f"{user_id}@test.local", name="top_failure_contexts 테스트 유저")
    )
    await session.flush()
    return user_id


async def _seed_tagged_failure_real(
    session: AsyncSession, *, user_id: UUID, tag_code: str, day: date, hour: int
) -> None:
    plan_start_at = datetime(day.year, day.month, day.day, hour, 0, tzinfo=KST)
    action_item_id = uuid4()
    session.add(
        ActionItem(
            id=action_item_id,
            user_id=user_id,
            title="top_failure_contexts 테스트 카드",
            target_date=plan_start_at.date(),
        )
    )
    await session.flush()

    block_id = uuid4()
    session.add(
        ScheduledBlock(
            id=block_id,
            user_id=user_id,
            action_item_id=action_item_id,
            start_at=plan_start_at,
            end_at=plan_start_at + timedelta(minutes=30),
        )
    )
    await session.flush()

    execution_id = uuid4()
    session.add(
        ExecutionEvent(
            id=execution_id,
            action_item_id=action_item_id,
            scheduled_block_id=block_id,
            user_id=user_id,
            plan_start_at=plan_start_at,
            plan_end_at=plan_start_at + timedelta(minutes=30),
            completion_status="failed",
        )
    )
    await session.flush()

    session.add(ExecutionFailureTag(id=uuid4(), execution_id=execution_id, tag_code=tag_code))
    await session.flush()


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")
async def test_get_top_failure_contexts_joins_label_ko_and_respects_limit(
    real_db_session: AsyncSession,
) -> None:
    """실 마스터 데이터(`failure_reason_tags`, 마이그레이션 시드)로 label_ko 조인을 확인하고,

    LIMIT 3 뒤에도 share 분모가 태그 전체(여기선 2개)를 유지하는지 본다.
    """

    from reaction_backend.repositories.review_repo import ReviewRepo

    user_id = await _seed_user_real(real_db_session)
    for hour in (9, 9, 14):
        await _seed_tagged_failure_real(
            real_db_session, user_id=user_id, tag_code="AMBIGUITY", day=date(2026, 8, 1), hour=hour
        )
    await _seed_tagged_failure_real(
        real_db_session, user_id=user_id, tag_code="FATIGUE", day=date(2026, 8, 1), hour=10
    )

    repo = ReviewRepo(real_db_session)
    rows = await repo.get_top_failure_contexts(user_id, date(2026, 8, 1), date(2026, 8, 1))

    assert [r.tag_code for r in rows] == ["AMBIGUITY", "FATIGUE"]
    assert [r.count for r in rows] == [3, 1]
    # label_ko 가 하드코딩이 아니라 마스터 테이블에서 실제로 조인돼 왔는지 — 빈 문자열이면
    # 조인이 죽어 있다는 뜻(빈 문자열은 SQL 이 안 잡아내는 실패라 값 자체를 확인해야 한다).
    assert all(r.label_ko for r in rows)
    assert sum(r.share for r in rows) == pytest.approx(1.0)


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")
async def test_get_top_failure_contexts_empty_when_no_failures(
    real_db_session: AsyncSession,
) -> None:

    from reaction_backend.repositories.review_repo import ReviewRepo

    user_id = await _seed_user_real(real_db_session)
    repo = ReviewRepo(real_db_session)
    rows = await repo.get_top_failure_contexts(user_id, date(2026, 8, 1), date(2026, 8, 1))
    assert rows == []
