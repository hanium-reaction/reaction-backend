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
) -> ScheduledBlock:
    b = ScheduledBlock()
    b.id = uuid4()
    b.user_id = DEMO_USER_UUID
    b.action_item_id = action_item_id
    b.start_at = start_at
    b.end_at = start_at + timedelta(minutes=30)
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
