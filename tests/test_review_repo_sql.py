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
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.recovery_attempt import (
    ADOPTED_DECISION_VALUES,
    RecoveryAttempt,
)
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
    session: AsyncSession,
    *,
    user_id: UUID,
    tag_code: str,
    day: date,
    hour: int,
    goal_id: UUID | None = None,
) -> None:
    plan_start_at = datetime(day.year, day.month, day.day, hour, 0, tzinfo=KST)
    action_item_id = uuid4()
    session.add(
        ActionItem(
            id=action_item_id,
            user_id=user_id,
            title="top_failure_contexts 테스트 카드",
            target_date=plan_start_at.date(),
            goal_id=goal_id,
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


# ─────────── 분 가중 지표 재료 (ADR-0009 D5) ───────────


def test_span_minutes_derives_planned_length_from_the_block_times() -> None:
    """계획 길이는 `plan_end_at - plan_start_at` — 카드의 estimated_minutes 가 아니다.

    사용자가 주간 편집기로 블록을 리사이즈하면 둘이 갈라지는데, 그 주에 실제로 계획돼
    있던 시간은 블록 쪽이다. `execution_events` 는 실행 시점의 블록 시각을 그대로 박아둔다.
    """
    from datetime import datetime, timedelta

    from reaction_backend.repositories.review_repo import _span_minutes
    from reaction_backend.schemas.common import KST

    start = datetime(2026, 6, 17, 19, 0, tzinfo=KST)
    assert _span_minutes(start, start + timedelta(minutes=90)) == 90
    assert _span_minutes(start, start + timedelta(minutes=15)) == 15
    # 끝이 없거나(진행 중 스냅샷) 뒤집혀 있으면 '모름' — 0 으로 세면 합계를 조용히 왜곡한다.
    assert _span_minutes(start, None) is None
    assert _span_minutes(None, start) is None
    assert _span_minutes(start, start) is None
    assert _span_minutes(start, start - timedelta(minutes=10)) is None


# ═══════ 계획 분해가 쓰는 이력 — 목표 단위 실패 집계 · 전략별 회복 결과 (실 Postgres) ═══════
#
# 사용자 전체 집계(위)와 **다른 값을 내는지**가 핵심이라 실 DB 로 본다. fake 로는 "목표로
# 좁혔다" 는 주장 자체를 fake 가 흉내내게 되므로 원리적으로 답이 안 나온다.


async def _seed_goal_real(session: AsyncSession, *, user_id: UUID, title: str) -> UUID:
    goal_id = uuid4()
    session.add(
        Goal(
            id=goal_id,
            user_id=user_id,
            title=title,
            category="study",
            goal_tier="focus",
        )
    )
    await session.flush()
    return goal_id


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")
async def test_get_goal_failure_contexts_counts_only_this_goals_failures(
    real_db_session: AsyncSession,
) -> None:
    """다른 목표에서 쌓인 태그가 이 목표의 분해 근거로 새어 들어오지 않는다.

    이게 목표 단위 집계를 만든 이유 전부다 — 사용자 전체 집계(같은 유저, 같은 창)는 두
    목표의 태그를 **함께** 세는데, 이 쿼리는 `action_items.goal_id` 로 갈라야 한다.
    """
    from reaction_backend.repositories.review_repo import ReviewRepo

    user_id = await _seed_user_real(real_db_session)
    mine = await _seed_goal_real(real_db_session, user_id=user_id, title="이 목표")
    other = await _seed_goal_real(real_db_session, user_id=user_id, title="다른 목표")

    for _ in range(2):
        await _seed_tagged_failure_real(
            real_db_session,
            user_id=user_id,
            tag_code="PLAN_TOO_BIG",
            day=date(2026, 8, 1),
            hour=9,
            goal_id=mine,
        )
    for _ in range(5):
        await _seed_tagged_failure_real(
            real_db_session,
            user_id=user_id,
            tag_code="FATIGUE",
            day=date(2026, 8, 1),
            hour=10,
            goal_id=other,
        )

    repo = ReviewRepo(real_db_session)
    scoped = await repo.get_goal_failure_contexts(user_id, mine, date(2026, 8, 1), date(2026, 8, 1))
    assert [(r.tag_code, r.count) for r in scoped] == [("PLAN_TOO_BIG", 2)]
    assert all(r.label_ko for r in scoped)  # 마스터 조인이 살아 있는지 (빈 문자열이면 죽은 것)

    # 대조군 — 같은 창의 사용자 전체 집계는 두 목표를 함께 센다. 두 쿼리가 같은 값을 내면
    # goal_id 조건이 아무 일도 안 하고 있다는 뜻이라, 이 대조가 있어야 테스트가 의미를 갖는다.
    overall = await repo.get_top_failure_contexts(user_id, date(2026, 8, 1), date(2026, 8, 1))
    assert [(r.tag_code, r.count) for r in overall] == [("FATIGUE", 5), ("PLAN_TOO_BIG", 2)]


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")
async def test_get_goal_failure_contexts_ignores_untagged_and_out_of_window(
    real_db_session: AsyncSession,
) -> None:
    """목표에 달린 실패라도 28일 창 밖이면 안 센다 — 창은 사용자 전체 집계와 같은 규약."""
    from reaction_backend.repositories.review_repo import ReviewRepo

    user_id = await _seed_user_real(real_db_session)
    goal_id = await _seed_goal_real(real_db_session, user_id=user_id, title="창 테스트 목표")
    await _seed_tagged_failure_real(
        real_db_session,
        user_id=user_id,
        tag_code="OVERRUN",
        day=date(2026, 6, 1),  # 기준일에서 28일보다 앞
        hour=9,
        goal_id=goal_id,
    )

    repo = ReviewRepo(real_db_session)
    rows = await repo.get_goal_failure_contexts(
        user_id, goal_id, date(2026, 8, 1), date(2026, 8, 1)
    )
    assert rows == []


async def _seed_recovery_attempt_real(
    session: AsyncSession,
    *,
    user_id: UUID,
    strategy_type: str,
    decision: str,
    result: str,
    decided_on: date,
) -> None:
    """회복 시도 1건 — 실행(execution) 한 건에 매달아 둔다(FK not null)."""
    plan_start_at = datetime(decided_on.year, decided_on.month, decided_on.day, 9, 0, tzinfo=KST)
    action_item_id = uuid4()
    session.add(
        ActionItem(
            id=action_item_id,
            user_id=user_id,
            title="회복 결과 테스트 카드",
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

    session.add(
        RecoveryAttempt(
            id=uuid4(),
            user_id=user_id,
            execution_id=execution_id,
            recovery_option_group="DOWNSCOPE",
            recovery_strategy_type=strategy_type,
            user_decision=decision,
            recovery_result=result,
            recovery_decided_at=plan_start_at + timedelta(hours=12),
        )
    )
    await session.flush()


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")
async def test_list_recovery_outcome_contexts_splits_worked_from_rejected(
    real_db_session: AsyncSession,
) -> None:
    """수락+완주 / 수락+중도포기 / 거절이 서로 다른 버킷으로 갈린다.

    'edited'(AI 문구를 고쳐 수락)도 'worked' 로 센다 — `ADOPTED_DECISION_VALUES` 를 안 쓰고
    "accepted" 만 비교하면 편집 수락이 조용히 빠지는데, resilience 분자에서 이미 겪은 버그다.
    """
    from reaction_backend.repositories.recovery_repo import RecoveryRepo

    user_id = await _seed_user_real(real_db_session)
    day = date(2026, 8, 1)
    for decision, result, strategy in (
        ("accepted", "completed", "NANO_STEP"),
        ("edited", "completed", "NANO_STEP"),
        ("accepted", "abandoned", "DOWNSCOPE_DEFAULT"),
        ("accepted", "abandoned", "DOWNSCOPE_DEFAULT"),
        ("rejected", "pending", "ENVIRONMENT_SHIFT"),
        ("rejected", "pending", "ENVIRONMENT_SHIFT"),
        ("pending", "pending", "NANO_STEP"),  # 아직 결정 안 함 — 어느 버킷도 아니다
    ):
        await _seed_recovery_attempt_real(
            real_db_session,
            user_id=user_id,
            strategy_type=strategy,
            decision=decision,
            result=result,
            decided_on=day,
        )

    rows = await RecoveryRepo(real_db_session).list_recovery_outcome_contexts(user_id, day, day)
    got = {(r.strategy_type, r.outcome): r.count for r in rows}
    assert got == {
        ("NANO_STEP", "worked"): 2,
        ("DOWNSCOPE_DEFAULT", "abandoned"): 2,
        ("ENVIRONMENT_SHIFT", "rejected"): 2,
    }
    assert all(r.label_ko for r in rows)  # 카탈로그 조인 (라벨 이중 관리 방지)


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")
async def test_list_recovery_outcome_contexts_respects_the_28_day_window(
    real_db_session: AsyncSession,
) -> None:
    """창 밖의 회복 결정은 안 센다 — 실패 집계와 같은 28일이라야 프롬프트 한 줄이 한 뜻이 된다."""
    from reaction_backend.repositories.recovery_repo import RecoveryRepo

    user_id = await _seed_user_real(real_db_session)
    await _seed_recovery_attempt_real(
        real_db_session,
        user_id=user_id,
        strategy_type="NANO_STEP",
        decision="accepted",
        result="completed",
        decided_on=date(2026, 6, 1),
    )

    rows = await RecoveryRepo(real_db_session).list_recovery_outcome_contexts(
        user_id, date(2026, 8, 1), date(2026, 8, 1)
    )
    assert rows == []
