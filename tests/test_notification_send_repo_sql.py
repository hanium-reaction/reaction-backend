"""알림 발송 repo 의 실 SQL 고정 — fake 전면대체 함정 대응 (#20).

`FakeNotificationSendRepo`/`FakeExecutionRepo` 가 sweep·게이트 테스트를 전부 받아내므로
실 repo 의 WHERE 는 **전 스위트에서 한 번도 실행되지 않는다**. 예산 카운트에 클래스
필터를 몰래 넣거나(주 3건 → 클래스별 3건으로 완화), pre_card 후보에서 사용자 활성
조건을 빼도 CI 는 초록이다 — 그래서 값까지 인라인(`literal_binds`)한 실 SQL 문자열로
고정한다 (`test_review_repo_sql.py` 에서 확립한 패턴).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from reaction_backend.schemas.common import KST

NOW = datetime(2026, 7, 21, 21, 0, tzinfo=KST)


class _RecordingResult:
    def all(self) -> list[Any]:
        return []

    def scalars(self) -> _RecordingResult:
        return self

    def scalar_one(self) -> int:
        return 0

    def unique(self) -> _RecordingResult:
        return self

    def __iter__(self) -> Any:
        return iter([])


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


# ── NotificationSendRepo ──


async def test_weekly_count_has_no_class_filter() -> None:
    """예산 카운트는 **전 클래스 합산** — notification_class 필터가 끼면 잠금 완화다."""
    from reaction_backend.repositories.notification_send_repo import NotificationSendRepo

    session = _RecordingSession()
    repo = NotificationSendRepo(session)  # type: ignore[arg-type]
    await repo.count_sent_since(uuid4(), since=NOW)

    sql = _sql(session.statements[0])
    assert "notification_sends.sent_at >=" in sql
    assert "notification_sends.user_id =" in sql
    assert "notification_class" not in sql, f"예산 카운트에 클래스 필터가 끼었다(잠금 완화): {sql}"


async def test_class_dedup_filters_by_class_and_since() -> None:
    from reaction_backend.repositories.notification_send_repo import NotificationSendRepo

    session = _RecordingSession()
    repo = NotificationSendRepo(session)  # type: ignore[arg-type]
    await repo.class_sent_since(uuid4(), notification_class="evening_reflection", since=NOW)

    sql = _sql(session.statements[0])
    assert "notification_sends.notification_class = 'evening_reflection'" in sql
    assert "notification_sends.sent_at >=" in sql


def test_send_repo_is_insert_only() -> None:
    """발송 이력은 게이트 enforce 의 근거 — 수정/삭제 메서드가 생기면 여기서 멈춘다."""
    from reaction_backend.repositories.notification_send_repo import NotificationSendRepo

    mutators = [
        name
        for name in dir(NotificationSendRepo)
        if not name.startswith("_") and ("update" in name or "delete" in name)
    ]
    assert mutators == [], f"INSERT-only 원칙 위반 후보: {mutators}"


# ── ExecutionRepo.list_blocks_starting_between (pre_card 후보) ──


async def test_pre_card_candidates_sql_pins_all_filters() -> None:
    """fake 가 못 지키는 조건까지 실 SQL 로 고정 — 특히 **활성 사용자 3조건**.

    익명화/탈퇴 사용자 필터가 빠지면 남아있는 블록에 계속 푸시가 나간다 —
    fake 는 사용자 테이블 자체가 없어 이 회귀를 절대 못 잡는다.
    """
    from reaction_backend.repositories.execution_repo import ExecutionRepo

    session = _RecordingSession()
    repo = ExecutionRepo(session)  # type: ignore[arg-type]
    await repo.list_blocks_starting_between(start=NOW, end=datetime(2026, 7, 21, 21, 5, tzinfo=KST))

    sql = _sql(session.statements[0])
    assert "scheduled_blocks.block_status = 'scheduled'" in sql, f"started 제외가 풀렸다: {sql}"
    assert "scheduled_blocks.start_at >=" in sql
    assert "scheduled_blocks.start_at <" in sql, f"[start, end) 반개구간이 아니다: {sql}"
    assert "action_items.archived_at IS NULL" in sql, f"만료 카드 제외가 풀렸다: {sql}"
    assert "users.archived_at IS NULL" in sql, f"탈퇴 사용자 제외가 풀렸다: {sql}"
    assert "users.is_anonymized IS false" in sql, f"익명화 사용자 제외가 풀렸다: {sql}"
    assert "users.onboarding_state = 'ACTIVE'" in sql, f"비활성 사용자 제외가 풀렸다: {sql}"
