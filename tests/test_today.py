"""Today / Execution 조회 — 실 구현 (Issue #19-A, api-contract §10).

#19-A 범위: GET /today/agenda + GET /today/actions/{id} (조회만).
Focus 실행 로깅(start/pause/resume/check-ins)은 #19-B (scheduled_blocks 의존).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.daily_brief import DailyBrief
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.domain.missed_check_in import MISSED_CHECK_IN_DELAY
from reaction_backend.schemas.common import now_kst
from tests.conftest import (
    DEMO_USER_UUID,
    FakeActionItemRepo,
    FakeDailyBriefRepo,
    FakeExecutionRepo,
)

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _today():  # noqa: ANN202
    return now_kst().date()


def _make_action(
    *, title: str = "캡스톤 1단계", priority: int = 3, source: str = "manual"
) -> ActionItem:
    a = ActionItem()
    a.id = uuid4()
    a.user_id = DEMO_USER_UUID
    a.title = title
    a.target_date = _today()
    a.category = "project"
    a.source = source
    a.status = "planned"
    a.priority = priority
    a.estimated_minutes = 30
    a.why_now = "마감이 다가와요"
    a.first_step = "노트북 열기"
    a.goal_id = None
    a.archived_at = None
    return a


def _make_brief(big_rock_id=None) -> DailyBrief:  # noqa: ANN001
    b = DailyBrief()
    b.id = uuid4()
    b.user_id = DEMO_USER_UUID
    b.brief_date = _today()
    b.headline_text = "오늘은 캡스톤에 집중해요"
    b.big_rock_action_item_id = big_rock_id
    b.adjustment_hints = [{"text": "오후 2시 회의 전에 마무리"}]
    b.fallback_used = False
    b.generated_at = datetime.now(UTC)
    b.expires_at = datetime.now(UTC) + timedelta(days=1)
    return b


def _seed_block(
    repo: FakeExecutionRepo,
    action_item_id,  # noqa: ANN001
    *,
    start_at: datetime,
    status: str = "scheduled",
    minutes: int = 30,
) -> ScheduledBlock:
    b = ScheduledBlock()
    b.id = uuid4()
    b.user_id = DEMO_USER_UUID
    b.action_item_id = action_item_id
    b.start_at = start_at
    b.end_at = start_at + timedelta(minutes=minutes)
    b.block_status = status
    b.source = "ai_plan"
    repo._blocks[b.id] = b
    return b


# ───── agenda ─────


def test_agenda_empty(client: TestClient) -> None:
    resp = client.get("/today/agenda")
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == _today().isoformat()
    assert body["brief"] is None
    assert body["cards"] == []
    assert body["habits"] == []
    assert body["fixedSchedules"] == []


def test_agenda_with_cards(client: TestClient, fake_action_item_repo: FakeActionItemRepo) -> None:
    fake_action_item_repo.seed(_make_action(title="토익 단어", priority=2))
    fake_action_item_repo.seed(_make_action(title="캡스톤 설계", priority=1))
    resp = client.get("/today/agenda")
    assert resp.status_code == 200
    cards = resp.json()["cards"]
    assert len(cards) == 2
    # priority 오름차순 — 1이 먼저
    assert cards[0]["title"] == "캡스톤 설계"
    assert cards[0]["actionId"].startswith("action_")
    assert cards[0]["whyNow"] == "마감이 다가와요"


# ── missedCheckIn (근거 대장 §6.2 T1, reaction-frontend#224) ──────────────


def test_agenda_flags_missed_check_in_past_the_delay(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    card = _make_action(title="놓친 카드")
    fake_action_item_repo.seed(card)
    _seed_block(
        fake_execution_repo,
        card.id,
        start_at=now_kst() - MISSED_CHECK_IN_DELAY - timedelta(minutes=1),
        status="scheduled",
    )

    body = client.get("/today/agenda").json()

    assert body["cards"][0]["missedCheckIn"] is True


def test_agenda_does_not_flag_card_still_within_the_delay(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    card = _make_action(title="방금 지난 카드")
    fake_action_item_repo.seed(card)
    _seed_block(
        fake_execution_repo, card.id, start_at=now_kst() - timedelta(minutes=5), status="scheduled"
    )

    body = client.get("/today/agenda").json()

    assert body["cards"][0]["missedCheckIn"] is False


def test_agenda_flags_a_short_block_before_the_fixed_20_minutes(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """15분짜리 블록은 +6분이면 미체크다 — 고정 20분이면 이미 끝난 뒤에야 떴다 (ADR-0009 D5).

    유예가 블록보다 길면 배지가 "지금 시작하라" 가 아니라 "이미 지나갔다" 는 사후 통보가 된다.
    """
    card = _make_action(title="짧은 카드")
    fake_action_item_repo.seed(card)
    _seed_block(
        fake_execution_repo,
        card.id,
        start_at=now_kst() - timedelta(minutes=6),
        status="scheduled",
        minutes=15,
    )

    body = client.get("/today/agenda").json()

    assert body["cards"][0]["missedCheckIn"] is True


def test_agenda_keeps_the_20_minute_cap_for_long_blocks(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """2시간짜리 블록도 유예는 최대 20분 — 근거 대장 §6.2 T1 의 선을 넘지 않는다.

    비례만 적용하면 120 × 0.3 = 36분이 되어 잠금된 값보다 느슨해진다. 상한이 그걸 막는다.
    """
    card = _make_action(title="긴 카드")
    fake_action_item_repo.seed(card)
    _seed_block(
        fake_execution_repo,
        card.id,
        start_at=now_kst() - timedelta(minutes=15),
        status="scheduled",
        minutes=120,
    )

    body = client.get("/today/agenda").json()
    assert body["cards"][0]["missedCheckIn"] is False  # 15분 < 20분 상한

    fake_execution_repo._blocks.clear()
    _seed_block(
        fake_execution_repo,
        card.id,
        start_at=now_kst() - timedelta(minutes=21),
        status="scheduled",
        minutes=120,
    )
    body = client.get("/today/agenda").json()
    assert body["cards"][0]["missedCheckIn"] is True  # 21분 > 20분 상한


def test_agenda_does_not_flag_started_block_even_if_overdue(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """[▶ 시작] 을 이미 눌렀다 — 아무리 시간이 지나도 미체크가 아니다."""
    card = _make_action(title="이미 시작한 카드")
    fake_action_item_repo.seed(card)
    _seed_block(
        fake_execution_repo,
        card.id,
        start_at=now_kst() - timedelta(hours=2),
        status="started",
    )

    body = client.get("/today/agenda").json()

    assert body["cards"][0]["missedCheckIn"] is False


def test_agenda_card_without_any_block_is_not_missed(
    client: TestClient, fake_action_item_repo: FakeActionItemRepo
) -> None:
    """블록이 아예 없는 카드(아직 배치 전)는 판정 대상이 아니다."""
    fake_action_item_repo.seed(_make_action(title="블록 없는 카드"))

    body = client.get("/today/agenda").json()

    assert body["cards"][0]["missedCheckIn"] is False


def test_agenda_with_brief(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_daily_brief_repo: FakeDailyBriefRepo,
) -> None:
    card = _make_action(title="big rock")
    fake_action_item_repo.seed(card)
    fake_daily_brief_repo.seed(_make_brief(big_rock_id=card.id))
    resp = client.get("/today/agenda")
    brief = resp.json()["brief"]
    assert brief is not None
    assert brief["headline"] == "오늘은 캡스톤에 집중해요"
    assert brief["bigRockActionId"] == f"action_{card.id}"
    assert brief["adjustmentHints"] == ["오후 2시 회의 전에 마무리"]
    assert brief["fallbackUsed"] is False


def test_agenda_with_habit(client: TestClient) -> None:
    client.post(
        "/habits",
        json={
            "title": "운동",
            "category": "health",
            "frequencyPerWeek": 3,
            "minutesPerSession": 30,
            "timePreference": "morning",
            "priorityLevel": 2,
        },
    )
    habits = client.get("/today/agenda").json()["habits"]
    assert len(habits) == 1
    assert habits[0]["title"] == "운동"
    assert habits[0]["targetCount"] == 3
    assert habits[0]["doneCount"] == 0
    assert habits[0]["instanceId"].startswith("hinst_")


def test_agenda_with_todays_fixed_schedule(client: TestClient) -> None:
    today_key = _WEEKDAYS[_today().weekday()]
    client.post(
        "/fixed-schedules",
        json={
            "title": "오늘 수업",
            "daysOfWeek": [today_key],
            "startTime": "13:00",
            "endTime": "14:30",
        },
    )
    fixed = client.get("/today/agenda").json()["fixedSchedules"]
    assert len(fixed) == 1
    assert fixed[0]["title"] == "오늘 수업"
    assert fixed[0]["startTime"] == "13:00"


def test_agenda_excludes_other_weekday_fixed(client: TestClient) -> None:
    other = _WEEKDAYS[(_today().weekday() + 1) % 7]
    client.post(
        "/fixed-schedules",
        json={
            "title": "내일 수업",
            "daysOfWeek": [other],
            "startTime": "09:00",
            "endTime": "10:00",
        },
    )
    assert client.get("/today/agenda").json()["fixedSchedules"] == []


# ───── action detail ─────


def test_action_detail(client: TestClient, fake_action_item_repo: FakeActionItemRepo) -> None:
    card = _make_action(title="상세 카드")
    fake_action_item_repo.seed(card)
    resp = client.get(f"/today/actions/action_{card.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "상세 카드"
    assert body["actionId"] == f"action_{card.id}"
    assert body["targetDate"] == _today().isoformat()
    assert body["firstStep"] == "노트북 열기"


def test_action_detail_not_found(client: TestClient) -> None:
    resp = client.get("/today/actions/action_99999999-9999-4999-8999-999999999999")
    assert resp.status_code == 404
    assert resp.json()["code"] == "COMMON_NOT_FOUND"


def test_action_detail_bad_id(client: TestClient) -> None:
    resp = client.get("/today/actions/nonexistent")
    assert resp.status_code == 404


# ── executionId — 회복 재진입이 가짜 실패를 만들지 않게 (#454 후속) ─────────


def _seed_execution(
    repo: FakeExecutionRepo,
    action_item_id,  # noqa: ANN001
    *,
    status: str = "failed",
    created_at: datetime | None = None,
) -> ExecutionEvent:
    e = ExecutionEvent()
    e.id = uuid4()
    e.user_id = DEMO_USER_UUID
    e.action_item_id = action_item_id
    e.scheduled_block_id = None
    e.completion_status = status
    e.plan_start_at = now_kst()
    e.plan_end_at = now_kst() + timedelta(minutes=30)
    e.actual_start_at = now_kst()
    e.created_at = created_at or now_kst()
    repo._executions[e.id] = e
    return e


def test_agenda_card_carries_its_latest_execution_id(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """⚠️ **회복 재진입이 가짜 실패를 만들던 구멍을 막는 값이다.**

    FE 는 실패한 카드의 회복 화면에 다시 들어갈 때 executionId 가 필요하다. 이 값이
    없으면 메모리 맵만 보고, 새로고침으로 그게 비면 `POST /today/actions/{id}/start` 로
    **새 실행을 만들어** 곧바로 failed 로 체크인했다 — 회복 화면에 들어갈 때마다 가짜
    실패가 하나씩 늘었다(실측: 두 번 실패한 카드에 실행 4건). 그 숫자가 주간 리뷰
    준수율과 에스컬레이션 레벨을 함께 밀어 올린다.
    """
    card = _make_action(title="실패한 카드")
    fake_action_item_repo.seed(card)
    execution = _seed_execution(fake_execution_repo, card.id)

    body = client.get("/today/agenda").json()

    assert body["cards"][0]["executionId"] == f"exec_{execution.id}"


def test_agenda_card_returns_the_most_recent_execution(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """여러 번 실행한 카드는 **마지막** 것을 준다 — 옛 실행을 주면 회복이 엉뚱한 실패에 붙는다."""
    card = _make_action(title="여러 번 한 카드")
    fake_action_item_repo.seed(card)
    _seed_execution(fake_execution_repo, card.id, created_at=now_kst() - timedelta(hours=3))
    latest = _seed_execution(fake_execution_repo, card.id, created_at=now_kst())

    body = client.get("/today/agenda").json()

    assert body["cards"][0]["executionId"] == f"exec_{latest.id}"


def test_agenda_card_without_execution_has_none(
    client: TestClient, fake_action_item_repo: FakeActionItemRepo
) -> None:
    """아직 시작 안 한 카드는 null — FE 가 이 값으로 '시작 필요'를 가른다."""
    fake_action_item_repo.seed(_make_action(title="아직 안 한 카드"))

    body = client.get("/today/agenda").json()

    assert body["cards"][0]["executionId"] is None
