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

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from reaction_backend.db.models.recovery_attempt import ADOPTED_DECISION_VALUES
from reaction_backend.schemas.common import KST

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
