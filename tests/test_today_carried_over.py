"""자정을 넘겨도 하던 카드가 오늘 화면에 남는다 — `AgendaCard.carriedOver` (today-3).

어젠다는 `target_date == 오늘(KST)` 만 모은다. `target_date` 는 블록의 KST 시작일이고
`plan_scheduler` 는 블록을 자정 너머로도 놓는다(#252). 그래서:

- 23:40 에 시작한 카드가 00:00 이 되는 순간 오늘 화면에서 사라졌다 — 방금 하던 일을
  어디서 완료할지 모르게 된다(60초 폴링이 열려 있는 화면에서도 지운다).
- 23:30~00:30 블록을 00:05 에 늦게라도 시작하려 하면 카드가 이미 없었다.

두 층으로 고정한다: 라우트(fake repo)로 어젠다 합치기·표시를, 실 Postgres 로 WHERE 를.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.repositories.execution_repo import ExecutionRepo
from reaction_backend.scheduler.expire_reflections import pending_reflection_since
from reaction_backend.schemas.common import KST, now_kst
from tests.conftest import DB_AVAILABLE, DEMO_USER_UUID, FakeActionItemRepo, FakeExecutionRepo


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time(0, 0), tzinfo=KST)


def _card(*, title: str, target: date, status: str = "planned", priority: int = 3) -> ActionItem:
    a = ActionItem()
    a.id = uuid4()
    a.user_id = DEMO_USER_UUID
    a.title = title
    a.target_date = target
    a.category = "study"
    a.source = "manual"
    a.status = status
    a.priority = priority
    a.estimated_minutes = 60
    a.why_now = None
    a.first_step = None
    a.goal_id = None
    a.archived_at = None
    return a


def _block(
    repo: FakeExecutionRepo, card: ActionItem, *, start: datetime, end: datetime, status: str
) -> ScheduledBlock:
    b = ScheduledBlock()
    b.id = uuid4()
    b.user_id = card.user_id
    b.action_item_id = card.id
    b.start_at = start
    b.end_at = end
    b.block_status = status
    b.source = "ai_plan"
    b.external_calendar_event_id = None
    repo._blocks[b.id] = b
    return b


def _execution(
    repo: FakeExecutionRepo, card: ActionItem, block: ScheduledBlock, *, status: str
) -> ExecutionEvent:
    e = ExecutionEvent()
    e.id = uuid4()
    e.user_id = card.user_id
    e.action_item_id = card.id
    e.scheduled_block_id = block.id
    e.plan_start_at = block.start_at
    e.plan_end_at = block.end_at
    e.actual_start_at = block.start_at + timedelta(minutes=10)
    e.actual_end_at = None
    e.actual_duration_minutes = None
    e.pause_total_minutes = 0
    e.completion_status = status
    e.created_at = e.actual_start_at
    repo._executions[e.id] = e
    return e


def _cards_by_id(client: TestClient) -> dict[str, dict[str, Any]]:
    resp = client.get("/today/agenda")
    assert resp.status_code == 200, resp.text
    return {c["actionId"]: c for c in resp.json()["cards"]}


# ── 라우트: 어젠다에 합치기 ─────────────────────────────────────────────


def test_card_started_before_midnight_stays_on_today(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """어제 23:30 블록에서 시작해 체크인 전 — 오늘 카드 **뒤에** executionId 와 함께 남는다."""
    today = now_kst().date()
    midnight = _day_start(today)
    todays = _card(title="오늘 카드", target=today, priority=5)
    late = _card(title="어젯밤 시작한 카드", target=today - timedelta(days=1), status="in_progress")
    fake_action_item_repo.seed(todays)
    fake_action_item_repo.seed(late)
    block = _block(
        fake_execution_repo,
        late,
        start=midnight - timedelta(minutes=30),
        end=midnight - timedelta(minutes=1),  # 블록은 끝났어도 실행은 진행 중
        status="started",
    )
    running = _execution(fake_execution_repo, late, block, status="in_progress")

    resp = client.get("/today/agenda")
    cards = resp.json()["cards"]

    assert [c["title"] for c in cards] == ["오늘 카드", "어젯밤 시작한 카드"]
    carried = cards[1]
    assert carried["carriedOver"] is True
    assert carried["status"] == "in_progress"
    assert carried["executionId"] == f"exec_{running.id}"
    assert cards[0]["carriedOver"] is False


def test_block_crossing_midnight_can_still_be_started(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """어제 시작해 아직 안 끝난 블록의 카드 — 늦게라도 [▶ 시작] 할 수 있게 남는다."""
    today = now_kst().date()
    card = _card(title="자정 넘긴 블록", target=today - timedelta(days=1))
    fake_action_item_repo.seed(card)
    _block(
        fake_execution_repo,
        card,
        start=_day_start(today) - timedelta(minutes=30),
        end=now_kst() + timedelta(minutes=30),
        status="scheduled",
    )

    carried = _cards_by_id(client)[f"action_{card.id}"]

    assert carried["carriedOver"] is True
    assert carried["executionId"] is None


def test_finished_or_archived_or_ended_cards_do_not_carry_over(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """이어 보여줄 이유가 없는 어제 카드는 종전처럼 안 뜬다."""
    today = now_kst().date()
    yesterday = today - timedelta(days=1)
    midnight = _day_start(today)

    # 체크인까지 끝낸 카드
    checked = _card(title="체크인 끝", target=yesterday, status="done")
    fake_action_item_repo.seed(checked)
    b1 = _block(
        fake_execution_repo,
        checked,
        start=midnight - timedelta(minutes=30),
        end=midnight - timedelta(minutes=1),
        status="finished",
    )
    _execution(fake_execution_repo, checked, b1, status="done")

    # 진행 중이지만 보관된 카드
    archived = _card(title="보관됨", target=yesterday, status="in_progress")
    archived.archived_at = now_kst()
    fake_action_item_repo.seed(archived)
    b2 = _block(
        fake_execution_repo,
        archived,
        start=midnight - timedelta(minutes=30),
        end=midnight - timedelta(minutes=1),
        status="started",
    )
    _execution(fake_execution_repo, archived, b2, status="in_progress")

    # 자정 전에 끝난, 시작 안 한 블록
    ended = _card(title="어제 끝난 블록", target=yesterday)
    fake_action_item_repo.seed(ended)
    _block(
        fake_execution_repo,
        ended,
        start=midnight - timedelta(hours=2),
        end=midnight - timedelta(hours=1),
        status="scheduled",
    )

    # 회고 창(오늘+어제+그제)을 벗어난 진행 중 실행 — 만료 cron 의 몫
    stale_day = today - timedelta(days=4)
    stale = _card(title="창 밖", target=stale_day, status="in_progress")
    fake_action_item_repo.seed(stale)
    b3 = _block(
        fake_execution_repo,
        stale,
        start=_day_start(stale_day) + timedelta(hours=9),
        end=_day_start(stale_day) + timedelta(hours=10),
        status="started",
    )
    _execution(fake_execution_repo, stale, b3, status="in_progress")

    assert _cards_by_id(client) == {}


def test_card_dated_today_is_not_duplicated(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """오늘 날짜 카드는 진행 중이어도 한 번만, `carriedOver=false` 로."""
    today = now_kst().date()
    card = _card(title="오늘 진행 중", target=today, status="in_progress")
    fake_action_item_repo.seed(card)
    block = _block(
        fake_execution_repo,
        card,
        start=now_kst() - timedelta(minutes=20),
        end=now_kst() + timedelta(minutes=40),
        status="started",
    )
    _execution(fake_execution_repo, card, block, status="in_progress")

    cards = client.get("/today/agenda").json()["cards"]

    assert len(cards) == 1
    assert cards[0]["carriedOver"] is False


# ── 실 Postgres: WHERE 고정 ──────────────────────────────────────────────


pytestmark_db = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")


async def _seed_card(
    session: AsyncSession,
    user_id: UUID,
    *,
    title: str,
    target: date,
    block: tuple[datetime, datetime, str],
    execution: str | None = None,
    archived: bool = False,
) -> UUID:
    card = ActionItem(
        id=uuid.uuid4(),
        user_id=user_id,
        title=title,
        target_date=target,
        category="study",
        source="manual",
        status="in_progress" if execution == "in_progress" else "planned",
        estimated_minutes=60,
        archived_at=now_kst() if archived else None,
    )
    session.add(card)
    await session.flush()
    start, end, block_status = block
    b = ScheduledBlock(
        id=uuid.uuid4(),
        user_id=user_id,
        action_item_id=card.id,
        start_at=start,
        end_at=end,
        block_status=block_status,
        source="ai_plan",
    )
    session.add(b)
    await session.flush()
    if execution is not None:
        session.add(
            ExecutionEvent(
                user_id=user_id,
                action_item_id=card.id,
                scheduled_block_id=b.id,
                plan_start_at=start,
                plan_end_at=end,
                actual_start_at=start + timedelta(minutes=10),
                completion_status=execution,
            )
        )
        await session.flush()
    return card.id


@pytestmark_db
async def test_carried_over_query_on_real_postgres(real_db_session: AsyncSession) -> None:
    s = real_db_session
    now = now_kst()
    today = now.date()
    yesterday = today - timedelta(days=1)
    midnight = _day_start(today)

    user_id = uuid.uuid4()
    s.add(User(id=user_id, email=f"carry+{user_id}@test.local", name="carry"))
    other_id = uuid.uuid4()
    s.add(User(id=other_id, email=f"carry+{other_id}@test.local", name="other"))
    await s.flush()

    late_night = (midnight - timedelta(minutes=30), midnight - timedelta(minutes=1), "started")
    running = await _seed_card(
        s, user_id, title="진행 중", target=yesterday, block=late_night, execution="in_progress"
    )
    crossing = await _seed_card(
        s,
        user_id,
        title="자정 넘긴 블록",
        target=yesterday,
        block=(midnight - timedelta(minutes=30), now + timedelta(minutes=30), "scheduled"),
    )
    # ── 아래는 전부 제외돼야 한다 ──
    await s.flush()
    excluded = [
        await _seed_card(
            s, user_id, title="체크인 끝", target=yesterday, block=late_night, execution="done"
        ),
        await _seed_card(
            s,
            user_id,
            title="보관됨",
            target=yesterday,
            block=late_night,
            execution="in_progress",
            archived=True,
        ),
        await _seed_card(
            s,
            user_id,
            title="자정 전 끝난 블록",
            target=yesterday,
            block=(midnight - timedelta(hours=2), midnight - timedelta(hours=1), "scheduled"),
        ),
        await _seed_card(
            s,
            user_id,
            title="취소된 블록",
            target=yesterday,
            block=(midnight - timedelta(minutes=30), now + timedelta(minutes=30), "cancelled"),
        ),
        await _seed_card(
            s,
            user_id,
            title="오늘 카드",
            target=today,
            block=(midnight + timedelta(minutes=1), now + timedelta(hours=1), "started"),
            execution="in_progress",
        ),
        await _seed_card(
            s,
            user_id,
            title="창 밖",
            target=today - timedelta(days=4),
            block=(
                _day_start(today - timedelta(days=4)) + timedelta(hours=9),
                _day_start(today - timedelta(days=4)) + timedelta(hours=10),
                "started",
            ),
            execution="in_progress",
        ),
        await _seed_card(
            s,
            other_id,
            title="남의 카드",
            target=yesterday,
            block=late_night,
            execution="in_progress",
        ),
    ]

    found = await ExecutionRepo(s).list_carried_over_actions(
        user_id,
        today=today,
        day_start=midnight,
        since=pending_reflection_since(today),
        now=now,
    )

    ids = [a.id for a in found]
    assert set(ids) == {running, crossing}, [a.title for a in found]
    assert not set(ids) & set(excluded)
