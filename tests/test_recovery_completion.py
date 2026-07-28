"""회복 완료 스탬프 — average_recovery_minutes 의 유일한 생산자 (#20).

배경: `recovery_completed_at`·`recovery_duration_minutes`·`recovery_result` 를 쓰는 코드가
레포에 **0곳**이라, `review_repo.collect_recovery_stats` 가 읽는 '평균 회복 시간' KPI 가
영구히 빈 값이었다. 채택한 회복 카드(`resulting_action_item_id`)를 done/over_done 으로
마치면 그 RecoveryAttempt 에 완료를 기록한다 — duration = 완료 − 결정 시각(설계서 §5.16,
결정→회복 완주 경과 시간).

여기서 검증:
1. 실 repo 의 스탬프 로직(성공→completed+duration / 실패→abandoned+duration 없음 / 멱등)
2. 실 SELECT 의 WHERE(resulting_action_item_id + result='pending') — fake 우회 대응
3. 라우트 배선(check-in 이 실제로 생산자를 호출) — 누가 그 호출을 지워도 잡히게
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from reaction_backend.db.models.recovery_attempt import RecoveryAttempt
from reaction_backend.repositories.recovery_repo import RecoveryRepo
from reaction_backend.schemas.common import KST
from tests.conftest import DEMO_USER_UUID, FakeActionItemRepo, FakeRecoveryRepo

DECIDED = datetime(2026, 7, 22, 21, 0, tzinfo=KST)


# ── 단위: 실 repo 로직 (fake 세션으로 attempt 주입) ──


class _OneResult:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def scalar_one_or_none(self) -> Any:
        return self._obj

    def scalars(self) -> _OneResult:
        return self

    def all(self) -> list[Any]:
        return []


class _InjectSession:
    """complete_for_action 의 SELECT 가 돌려줄 attempt 를 주입하는 세션."""

    def __init__(self, attempt: RecoveryAttempt | None) -> None:
        self._attempt = attempt
        self.flushed = False

    async def execute(self, stmt: object) -> _OneResult:
        return _OneResult(self._attempt)

    async def flush(self) -> None:
        self.flushed = True


def _adopted_attempt(*, started: datetime | None = DECIDED) -> RecoveryAttempt:
    a = RecoveryAttempt()
    a.id = uuid4()
    a.user_id = uuid4()
    a.resulting_action_item_id = uuid4()
    a.recovery_started_at = started
    a.recovery_completed_at = None
    a.recovery_duration_minutes = None
    a.recovery_result = "pending"
    return a


async def _complete(attempt: RecoveryAttempt | None, *, done_at: datetime, status: str) -> Any:
    repo = RecoveryRepo(_InjectSession(attempt))  # type: ignore[arg-type]
    action_id = attempt.resulting_action_item_id if attempt else uuid4()
    user_id = attempt.user_id if attempt else uuid4()
    return await repo.complete_for_action(
        user_id, action_id, completed_at=done_at, completion_status=status
    )


@pytest.mark.parametrize("status", ["done", "over_done"])
async def test_success_stamps_completed_and_duration(status: str) -> None:
    a = _adopted_attempt()
    out = await _complete(a, done_at=DECIDED + timedelta(minutes=45), status=status)

    assert out is a
    assert a.recovery_result == "completed"
    assert a.recovery_completed_at == DECIDED + timedelta(minutes=45)
    assert a.recovery_duration_minutes == 45


async def test_carry_over_spanning_a_day_is_valid_duration() -> None:
    """CARRY_OVER 는 다음날 완료 → duration 이 하루를 넘어도 정상(결정→완주 경과)."""
    a = _adopted_attempt()
    out = await _complete(a, done_at=DECIDED + timedelta(days=1, hours=1), status="done")

    assert out is not None
    assert a.recovery_duration_minutes == 25 * 60


@pytest.mark.parametrize("status", ["failed", "partial_done"])
async def test_non_success_is_abandoned_without_duration(status: str) -> None:
    """실패·부분완료는 abandoned + duration 없음 → 평균 회복 시간에서 제외된다."""
    a = _adopted_attempt()
    await _complete(a, done_at=DECIDED + timedelta(minutes=30), status=status)

    assert a.recovery_result == "abandoned"
    assert a.recovery_completed_at is None
    assert a.recovery_duration_minutes is None


async def test_no_matching_attempt_is_noop() -> None:
    """대다수 카드는 회복이 아니다 — 매칭 attempt 없으면 None (조용히 통과)."""
    assert await _complete(None, done_at=DECIDED, status="done") is None


async def test_missing_started_at_skips_duration_but_marks_completed() -> None:
    """started_at 이 없으면(비정상) duration 은 못 재도 completed 로 기록은 한다."""
    a = _adopted_attempt(started=None)
    await _complete(a, done_at=DECIDED, status="done")

    assert a.recovery_result == "completed"
    assert a.recovery_duration_minutes is None


# ── 실 SQL: SELECT WHERE 고정 (fake 전면대체 대응) ──


class _RecordingSession:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, stmt: object) -> _OneResult:
        self.statements.append(stmt)
        return _OneResult(None)  # attempt 없음 → 변이 없이 SELECT 만 검사

    async def flush(self) -> None:
        return None


def _sql(stmt: object) -> str:
    from sqlalchemy.dialects import postgresql

    raw = str(
        stmt.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    return " ".join(raw.split())


async def test_abandon_stale_pins_where_and_set() -> None:
    """포기 처리 UPDATE 의 WHERE·SET 고정 — 이 경로는 fake 가 없어 실 SQL 이 전부다.

    지키는 것:
    - SET 은 `recovery_result='abandoned'` **하나뿐** — 다른 컬럼(특히 duration·completed_at)을
      건드리면 지표가 오염된다.
    - `recovery_result='pending'` 가드 = 멱등 + **completed 를 포기로 덮지 않음**(지표 파괴 방지).
    - 카드 status 가 done/over_done 이면 제외 — 실제로 해낸 회복을 포기로 만들지 않는다.
    - 조인 키가 `resulting_action_item_id` — 채택(ADOPTED) 필터의 유일한 근거.
    - **회고 창 기준식(`greatest(plan_start_at, actual_start_at)`) 가드** — 아직 회고할 수
      있는 회복을 포기로 확정하면 이후 완주 스탬프가 영영 막혀 KPI 가 사라진다.
    - `action_items` 는 **읽기만** — 원본 status 를 UPDATE 하면 AGENTS §2 위반.
    """
    from reaction_backend.repositories.recovery_repo import RecoveryRepo

    session = _RecordingSession()
    repo = RecoveryRepo(session)  # type: ignore[arg-type]
    await repo.abandon_stale(before=datetime(2026, 7, 22, 0, 0, tzinfo=KST))

    sql = _sql(session.statements[0])
    set_clause = sql.split(" WHERE ", 1)[0]

    assert sql.startswith("UPDATE recovery_attempts"), f"엉뚱한 테이블을 갱신한다: {sql}"
    assert "recovery_result='abandoned'" in set_clause, f"SET 이 abandoned 가 아니다: {set_clause}"
    # SET 에 결과 외 컬럼이 끼면 지표가 오염된다 (updated_at 은 TimestampMixin onupdate).
    assert "recovery_duration_minutes" not in set_clause, (
        f"SET 이 duration 을 건드린다: {set_clause}"
    )
    assert "recovery_completed_at" not in set_clause, (
        f"SET 이 completed_at 을 건드린다: {set_clause}"
    )

    assert "recovery_attempts.recovery_result = 'pending'" in sql, f"멱등 가드가 풀렸다: {sql}"
    assert "action_items.target_date < '2026-07-22'" in sql, f"창 경계가 안 걸렸다: {sql}"
    assert "action_items.status NOT IN ('done', 'over_done')" in sql, (
        f"완주 카드 제외가 풀렸다: {sql}"
    )
    # 조인 키 — execution_id 등 다른 컬럼으로 바뀌면 채택 필터가 통째로 무너진다.
    assert "recovery_attempts.resulting_action_item_id IN (SELECT action_items.id" in sql, (
        f"채택 필터(조인 키)가 풀렸다: {sql}"
    )
    # 회고 창 기준식 가드 — 만료 cron 과 **같은 식**이어야 여집합이 성립한다 (#20).
    assert "greatest(execution_events.plan_start_at" in sql, f"회고 창 기준식이 빠졌다: {sql}"
    assert "coalesce(execution_events.actual_start_at" in sql, f"실 착수 시각을 안 본다: {sql}"
    assert "NOT (EXISTS (SELECT execution_events.id" in sql, (
        f"회고 가능한 실행 제외 가드가 풀렸다: {sql}"
    )
    assert "UPDATE action_items" not in sql, "원본 카드 status 를 갱신한다 (AGENTS §2 위반)"


async def test_complete_for_action_select_pins_where() -> None:
    """실 SELECT 가 resulting_action_item_id + result='pending' 로 잡는다.

    - resulting_action_item_id 매칭 = 채택(ADOPTED) 카드만 (그 컬럼은 채택 시에만 채워짐)
    - result='pending' = **멱등** (이미 종결된 attempt 를 재체크인이 덮지 않음)
    fake 가 이 로직을 대체하므로 실 WHERE 는 이 테스트에서만 실행된다.
    """
    session = _RecordingSession()
    repo = RecoveryRepo(session)  # type: ignore[arg-type]
    await repo.complete_for_action(uuid4(), uuid4(), completed_at=DECIDED, completion_status="done")

    sql = _sql(session.statements[0])
    assert "recovery_attempts.resulting_action_item_id =" in sql
    assert "recovery_attempts.recovery_result = 'pending'" in sql, f"멱등 가드가 풀렸다: {sql}"
    assert "recovery_attempts.user_id =" in sql


async def test_abandon_cron_uses_the_same_window_as_expiry() -> None:
    """포기 판정 경계가 만료 cron과 **같은 단일 소스**(pending_reflection_since)다.

    회귀: 여기서 자체 상수를 쓰면 카드는 정리됐는데 회복 시도는 남거나(또는 반대로) 되어
    "3일 지나면 정리된다"는 사용자 관점이 깨진다. 잠금 결정(AGENTS §1 3일)에서 파생.

    경계는 **datetime 그대로** 넘긴다 — repo 가 만료 cron 과 같은 기준식
    (`greatest(plan_start_at, actual_start_at)`)에 비교하므로 date 로 잘라내면 안 된다.
    """
    from reaction_backend.scheduler.expire_reflections import (
        pending_reflection_since,
        run_abandon_stale_recoveries,
    )

    captured: dict[str, Any] = {}

    class _Repo:
        async def abandon_stale(self, *, before: Any) -> int:
            captured["before"] = before
            return 0

    class _Session:
        async def commit(self) -> None:
            return None

    now = datetime(2026, 7, 24, 4, 0, tzinfo=KST)
    await run_abandon_stale_recoveries(_Session(), now=now, repo=_Repo())  # type: ignore[arg-type]

    assert captured["before"] == pending_reflection_since(now.date())
    assert captured["before"] == datetime(2026, 7, 22, 0, 0, tzinfo=KST), (
        "3일 창 경계가 아니다 (7/22·23·24 는 살아있어야)"
    )


def test_expire_job_also_abandons_stale_recoveries() -> None:
    """04:00 job 이 만료와 포기 처리를 **둘 다** 부른다 — 배선 guard.

    회귀: runtime 에서 abandon 호출을 지우면 카드만 정리되고 회복 시도는 영영 pending 이다.
    """
    import inspect

    from reaction_backend.scheduler import runtime

    src = inspect.getsource(runtime._expire_reflections_job)
    assert "run_expire_unreflected_cards" in src
    assert "run_abandon_stale_recoveries" in src, "만료 job 이 회복 포기 처리를 안 부른다"


# ── 라우트 배선: check-in 이 생산자를 부른다 (누가 지워도 잡힘) ──


def _seed_recovery_card(action_repo: FakeActionItemRepo) -> Any:
    from reaction_backend.db.models.action_item import ActionItem

    a = ActionItem()
    a.id = uuid4()
    a.user_id = DEMO_USER_UUID
    a.title = "회복: 5분만 시작"
    a.target_date = date(2026, 7, 22)
    a.category = "study"
    a.source = "recovery_downscope"
    a.status = "planned"
    a.priority = 3
    a.estimated_minutes = 15
    a.why_now = None
    a.first_step = None
    a.goal_id = None
    a.parent_action_item_id = uuid4()
    a.archived_at = None
    action_repo.seed(a)
    return a


def _seed_adopted_attempt(
    recovery_repo: FakeRecoveryRepo, *, resulting_action_id: Any
) -> RecoveryAttempt:
    at = RecoveryAttempt()
    at.id = uuid4()
    at.user_id = DEMO_USER_UUID
    at.execution_id = uuid4()
    at.recovery_option_group = "DOWNSCOPE"
    at.recovery_strategy_type = "NANO_STEP"
    at.suggested_action_text = "5분만"
    at.trigger_tag = "AMBIGUITY"
    at.llm_fallback_used = False
    at.user_decision = "accepted"
    at.recovery_decided_at = DECIDED
    at.recovery_started_at = DECIDED  # 결정 시각 = 회복 시작 (실 라우트가 이렇게 설정)
    at.recovery_completed_at = None
    at.recovery_duration_minutes = None
    at.recovery_result = "pending"
    at.resulting_action_item_id = resulting_action_id
    recovery_repo._attempts[at.id] = at
    return at


def test_check_in_done_stamps_recovery_completion(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_recovery_repo: FakeRecoveryRepo,
) -> None:
    """회복 카드를 done 으로 체크인하면 그 RecoveryAttempt 가 완료로 스탬프된다.

    회귀: today.py 의 `recovery_repo.complete_for_action(...)` 호출을 지우면 이 테스트가
    깨진다 — 지우면 average_recovery_minutes 가 다시 영구 빈 값이 되기 때문.
    """
    card = _seed_recovery_card(fake_action_item_repo)
    attempt = _seed_adopted_attempt(fake_recovery_repo, resulting_action_id=card.id)

    start = client.post(f"/today/actions/action_{card.id}/start")
    assert start.status_code == 201, start.text
    execution_id = start.json()["executionId"]

    resp = client.post(
        "/today/check-ins",
        json={"executionId": execution_id, "completionStatus": "done"},
    )
    assert resp.status_code == 200, resp.text

    assert attempt.recovery_result == "completed"
    assert attempt.recovery_completed_at is not None
    assert attempt.recovery_duration_minutes is not None
    assert attempt.recovery_duration_minutes >= 0


def test_check_in_failed_recovery_card_marks_abandoned(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_recovery_repo: FakeRecoveryRepo,
) -> None:
    """회복 카드를 실패로 체크인하면 abandoned — 평균 회복 시간엔 안 들어간다."""
    card = _seed_recovery_card(fake_action_item_repo)
    attempt = _seed_adopted_attempt(fake_recovery_repo, resulting_action_id=card.id)

    start = client.post(f"/today/actions/action_{card.id}/start")
    execution_id = start.json()["executionId"]
    client.post(
        "/today/check-ins",
        json={"executionId": execution_id, "completionStatus": "failed"},
    )

    assert attempt.recovery_result == "abandoned"
    assert attempt.recovery_duration_minutes is None


def test_reflection_batch_done_stamps_recovery_completion(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_recovery_repo: FakeRecoveryRepo,
) -> None:
    """저녁 일괄 회고로 회복 카드를 마쳐도 스탬프된다 — reflection.py 배선 guard.

    회귀: reflection.py 의 `complete_for_action(...)` 호출을 지우면 이 테스트가 깨진다
    (Focus 체크인 말고 저녁 batch 로 회복 카드를 마치는 경로도 지표에 잡혀야 한다).
    """
    card = _seed_recovery_card(fake_action_item_repo)
    attempt = _seed_adopted_attempt(fake_recovery_repo, resulting_action_id=card.id)
    execution_id = client.post(f"/today/actions/action_{card.id}/start").json()["executionId"]

    resp = client.post(
        "/reflection/batch",
        json={"items": [{"executionId": execution_id, "completionStatus": "done"}]},
        headers={"Idempotency-Key": f"batch-{uuid4()}"},
    )
    assert resp.status_code == 200, resp.text

    assert attempt.recovery_result == "completed"
    assert attempt.recovery_duration_minutes is not None
