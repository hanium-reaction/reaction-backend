"""카드 취소 — `POST /today/actions/{id}/cancel` (BE #214).

배경: 잘못 담긴 카드를 되돌릴 방법이 없었다. 체크인으로 처리하면 하지도 않은 실행
기록이 생기고, 방치하면 `planned` 로 영구 잔류한다(`expire_unreflected` 는 in_progress
실행만 본다). #213 의 중복 카드를 사용자가 지울 수 없었던 것도 이 공백 때문이다.

이 테스트가 지키는 것:
- **판정이 한 곳에만 있다** — 어젠다의 `cancellable` 과 라우트 가드가 같은 답을 낸다.
  두 곳에 두면 FE 가 버튼을 띄워 놓고 눌렀을 때 422 를 받는 상태로 조용히 어긋난다.
- **`status` 는 안 바뀐다** — AGENTS §2(원본 status = Resilience 지표 전제).
- **멱등** — FE 는 5초 스낵바 뒤에 호출하므로 재시도가 실패로 보이면 안 된다.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.domain import action_cancel
from reaction_backend.repositories.action_item_repo import ActionItemRepo
from reaction_backend.schemas.common import KST, now_kst
from tests.conftest import (
    DB_AVAILABLE,
    DEMO_USER_UUID,
    FakeActionItemRepo,
    FakeExecutionRepo,
    FakeScheduledBlockRepo,
)


def _make_card(*, source: str = "inbox", status: str = "planned") -> ActionItem:
    a = ActionItem()
    a.id = uuid4()
    a.user_id = DEMO_USER_UUID
    a.title = "5분짜리 최소 크기 정해두기"
    a.target_date = now_kst().date()
    a.category = "health"
    a.source = source
    a.status = status
    a.priority = 3
    a.estimated_minutes = 30
    a.why_now = None
    a.first_step = None
    a.goal_id = None
    a.archived_at = None
    return a


def _seed_execution(repo: FakeExecutionRepo, card: ActionItem, *, status: str) -> None:
    e = ExecutionEvent()
    e.id = uuid4()
    e.user_id = card.user_id
    e.action_item_id = card.id
    e.scheduled_block_id = uuid4()
    e.plan_start_at = now_kst()
    e.plan_end_at = now_kst()
    e.completion_status = status
    repo._executions[e.id] = e


def _agenda_card(client: TestClient, card: ActionItem) -> dict | None:  # noqa: ANN401
    cards = client.get("/today/agenda").json()["cards"]
    return next((c for c in cards if c["actionId"] == f"action_{card.id}"), None)


# ── 도메인 규칙 (프레임워크 없이) ─────────────────────────


@pytest.mark.parametrize(
    ("status", "source", "history", "expected"),
    [
        ("planned", "inbox", False, True),
        ("planned", "manual", False, True),
        # 시작한 카드는 실행 이력이 지표의 근거다
        ("in_progress", "inbox", False, False),
        ("done", "inbox", False, False),
        # status 가 planned 로 돌아와 있어도 이력이 있으면 '없던 일' 이 아니다
        ("planned", "inbox", True, False),
        # 회복 파생은 resulting_action_item_id 로 회복 지표와 얽혀 있다
        ("planned", "recovery_downscope", False, False),
        ("planned", "recovery_carryover", False, False),
        # 계획/습관 파생은 주간 그리드·습관 카운트가 걸려 있어 별건
        ("planned", "goal", False, False),
        ("planned", "habit", False, False),
    ],
)
def test_is_cancellable_truth_table(
    status: str, source: str, history: bool, expected: bool
) -> None:
    assert (
        action_cancel.is_cancellable(status=status, source=source, has_execution_history=history)
        is expected
    )


def test_rejection_reason_distinguishes_started_from_unsupported_source() -> None:
    """FE 가 사유별 문구를 띄울 수 있어야 한다 — 공용 '입력값 확인' 은 여기서 거짓말이다."""
    started = action_cancel.rejection_reason(
        status="in_progress", source="inbox", has_execution_history=True
    )
    wrong_source = action_cancel.rejection_reason(
        status="planned", source="goal", has_execution_history=False
    )
    assert started is not None and wrong_source is not None
    assert started != wrong_source, "두 사유가 같은 문구면 FE 가 구분할 수 없다"
    assert (
        action_cancel.rejection_reason(
            status="planned", source="inbox", has_execution_history=False
        )
        is None
    )


# ── 라우트 ────────────────────────────────────────────────


def test_cancel_removes_the_card_from_today(
    client: TestClient, fake_action_item_repo: FakeActionItemRepo
) -> None:
    """이 기능의 전부 — 취소하면 오늘 화면에서 사라진다."""
    card = _make_card()
    fake_action_item_repo.seed(card)
    assert _agenda_card(client, card) is not None

    resp = client.post(f"/today/actions/action_{card.id}/cancel")
    assert resp.status_code == 204, resp.text
    assert _agenda_card(client, card) is None


def test_cancel_does_not_change_status(
    client: TestClient, fake_action_item_repo: FakeActionItemRepo
) -> None:
    """AGENTS §2 — 원본 status 는 Resilience 지표의 전제라 취소가 건드리면 안 된다.

    `archived_at` 만으로 목록·지표에서 빠지므로 status 를 바꿀 이유도 없다.
    """
    card = _make_card()
    fake_action_item_repo.seed(card)

    assert client.post(f"/today/actions/action_{card.id}/cancel").status_code == 204
    assert card.status == "planned", "취소가 status 를 바꿨다"
    assert card.archived_at is not None, "archived_at 이 안 찍혔다"


def test_cancel_is_idempotent(
    client: TestClient, fake_action_item_repo: FakeActionItemRepo
) -> None:
    """FE 는 5초 스낵바 뒤에 호출한다 — 재시도가 404 로 보이면 안 된다."""
    card = _make_card()
    fake_action_item_repo.seed(card)

    first = client.post(f"/today/actions/action_{card.id}/cancel")
    second = client.post(f"/today/actions/action_{card.id}/cancel")
    assert first.status_code == second.status_code == 204, second.text


def test_cancel_commits(
    client: TestClient, fake_action_item_repo: FakeActionItemRepo, fake_sessions: list
) -> None:
    """commit 이 없으면 204 를 주고도 카드가 그대로 남는다 — 조용한 실패."""
    card = _make_card()
    fake_action_item_repo.seed(card)
    before = sum(s.commit_count for s in fake_sessions)

    assert client.post(f"/today/actions/action_{card.id}/cancel").status_code == 204
    assert sum(s.commit_count for s in fake_sessions) > before, "cancel 이 commit 하지 않았다"


def test_cancel_rejects_a_card_with_execution_history(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """status 가 planned 로 남아 있어도 시작한 적이 있으면 '없던 일' 이 아니다."""
    card = _make_card()
    fake_action_item_repo.seed(card)
    _seed_execution(fake_execution_repo, card, status="failed")

    resp = client.post(f"/today/actions/action_{card.id}/cancel")
    assert resp.status_code == 422, resp.text
    assert resp.json()["field"] == "actionId"
    assert card.archived_at is None, "거절했는데 카드가 보관됐다"


@pytest.mark.parametrize("source", ["recovery_downscope", "goal", "habit"])
def test_cancel_rejects_unsupported_sources(
    client: TestClient, fake_action_item_repo: FakeActionItemRepo, source: str
) -> None:
    card = _make_card(source=source)
    fake_action_item_repo.seed(card)

    assert client.post(f"/today/actions/action_{card.id}/cancel").status_code == 422
    assert card.archived_at is None


def test_cancel_missing_card_is_404(client: TestClient) -> None:
    resp = client.post("/today/actions/action_99999999-9999-4999-8999-999999999999/cancel")
    assert resp.status_code == 404
    assert resp.json()["code"] == "COMMON_NOT_FOUND"


# ── 드리프트 방지: 어젠다 플래그 == 라우트 가드 ──────────
#
# FE 는 `cancellable` 만 보고 버튼을 그린다. 두 판정이 갈라지면 버튼을 눌렀는데 422 가
# 오거나(사용자가 이유를 알 수 없다), 취소할 수 있는 카드에 버튼이 없다.


@pytest.mark.parametrize(
    ("source", "status", "history"),
    [
        ("inbox", "planned", False),
        ("manual", "planned", False),
        ("inbox", "planned", True),
        ("inbox", "in_progress", False),
        ("recovery_carryover", "planned", False),
        ("goal", "planned", False),
    ],
)
def test_agenda_flag_matches_route_guard(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
    source: str,
    status: str,
    history: bool,
) -> None:
    card = _make_card(source=source, status=status)
    fake_action_item_repo.seed(card)
    if history:
        _seed_execution(fake_execution_repo, card, status="failed")

    flagged = _agenda_card(client, card)
    assert flagged is not None
    resp = client.post(f"/today/actions/action_{card.id}/cancel")

    assert flagged["cancellable"] is (resp.status_code == 204), (
        f"어젠다 플래그({flagged['cancellable']})와 라우트({resp.status_code})가 어긋났다"
    )


def test_agenda_flag_is_present_on_every_card(
    client: TestClient, fake_action_item_repo: FakeActionItemRepo
) -> None:
    """FE 가 optional 로 다루지 않도록 항상 실린다(파생 필드, DB 컬럼 아님)."""
    fake_action_item_repo.seed(_make_card())
    card = client.get("/today/agenda").json()["cards"][0]
    assert "cancellable" in card
    assert isinstance(card["cancellable"], bool)


# ── 블록도 함께 정리된다 (data-2) ─────────────────────────────────────────
#
# 카드만 보관하면 블록이 `scheduled` 로 영원히 남았다 — 주간 그리드에 취소한 카드
# 제목으로 뜨고(눌러 보면 404), 그 시간대는 계속 '바쁨' 이라 다른 블록을 옮겨 오면
# PLAN_BLOCK_CONFLICT 로 막혔다. 정리해 주는 cron 도 없다.


def _block_for(card: ActionItem, start: datetime, *, status: str = "scheduled") -> ScheduledBlock:
    b = ScheduledBlock()
    b.id = uuid4()
    b.user_id = card.user_id
    b.action_item_id = card.id
    b.start_at = start
    b.end_at = start + timedelta(minutes=30)
    b.block_status = status
    b.source = "ai_plan"
    b.external_calendar_event_id = None
    return b


def test_cancel_takes_the_block_off_the_weekly_grid(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> None:
    card = _make_card()
    other = _make_card()
    fake_action_item_repo.seed(card)
    fake_action_item_repo.seed(other)
    week_start = now_kst().date() - timedelta(days=now_kst().weekday())
    at = datetime.combine(week_start + timedelta(days=1), time(8, 0), tzinfo=KST)
    ghost = _block_for(card, at)
    kept = _block_for(other, at + timedelta(hours=2))
    fake_scheduled_block_repo.seed(ghost, title=card.title, category=card.category)
    fake_scheduled_block_repo.seed(kept, title=other.title, category=other.category)

    assert client.post(f"/today/actions/action_{card.id}/cancel").status_code == 204

    assert ghost.block_status == "cancelled"
    assert kept.block_status == "scheduled", "다른 카드의 블록까지 건드렸다"
    weekly = client.get("/plans/weekly", params={"weekStart": week_start.isoformat()}).json()
    listed = {b["blockId"] for d in weekly["days"] for b in d["blocks"]}
    assert f"block_{ghost.id}" not in listed, "취소한 카드의 블록이 주간 그리드에 남았다"
    assert f"block_{kept.id}" in listed

    # 다시 취소해도 204 — 멱등은 그대로
    assert client.post(f"/today/actions/action_{card.id}/cancel").status_code == 204


pytestmark_db = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")


@pytestmark_db
async def test_cancel_sql_cancels_only_this_cards_unfinished_blocks(
    real_db_session: AsyncSession,
) -> None:
    """실 Postgres — UPDATE 의 WHERE 가 이 카드·미종결 블록만 고르는가."""
    s = real_db_session
    user_id = uuid4()
    s.add(User(id=user_id, email=f"cancel+{user_id}@test.local", name="cancel"))
    await s.flush()

    def _card_row(title: str) -> ActionItem:
        return ActionItem(
            id=uuid4(),
            user_id=user_id,
            title=title,
            target_date=now_kst().date(),
            category="study",
            source="inbox",
            status="planned",
            estimated_minutes=30,
        )

    card, other = _card_row("취소할 카드"), _card_row("남길 카드")
    s.add_all([card, other])
    await s.flush()
    at = now_kst() + timedelta(days=1)
    rows = {
        "scheduled": _block_for(card, at, status="scheduled"),
        "finished": _block_for(card, at + timedelta(hours=1), status="finished"),
        "other": _block_for(other, at, status="scheduled"),
    }
    s.add_all(rows.values())
    await s.flush()

    await ActionItemRepo(s).cancel(card)
    await s.flush()

    statuses = dict(
        (
            await s.execute(
                select(ScheduledBlock.id, ScheduledBlock.block_status).where(
                    ScheduledBlock.id.in_([b.id for b in rows.values()])
                )
            )
        ).all()
    )
    assert statuses[rows["scheduled"].id] == "cancelled"
    assert statuses[rows["finished"].id] == "finished", "수행 이력 블록을 지웠다"
    assert statuses[rows["other"].id] == "scheduled", "다른 카드의 블록까지 건드렸다"
    assert card.archived_at is not None
    assert card.status == "planned", "취소가 status 를 바꿨다 (AGENTS §2)"
