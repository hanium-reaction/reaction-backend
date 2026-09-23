"""여러 날로 쪼갠 카드 — 둘째 날에도 오늘 화면에 뜨고, 끝내면 남은 회차가 정리된다 (critic-2).

주간 '남은 일 다시 배치' 는 긴 카드를 여러 날의 세션 블록으로 쪼갠다(`plan_scheduler`).
카드는 1장, `target_date` 는 가장 이른 블록의 날짜 하나뿐이라:

- 둘째 날부터는 그 카드가 오늘 화면에서 사라져 시작할 방법이 없었다.
- 첫 회차에서 '완료' 하면 카드는 done 인데 둘째 날 블록은 `scheduled` 로 남아, 주간표에
  할 일처럼 계속 뜨고 5분 전 '곧 시작' 알림까지 왔다.

라우트(fake)로 어젠다 표시를, 실 Postgres 로 세 쿼리(어젠다·체크인 정리·pre_card)를 고정한다.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.repositories.action_item_repo import ActionItemRepo
from reaction_backend.repositories.execution_repo import ExecutionRepo
from reaction_backend.schemas.common import KST, now_kst
from tests.conftest import (
    DB_AVAILABLE,
    DEMO_USER_UUID,
    FakeActionItemRepo,
    FakeScheduledBlockRepo,
)

D1 = date(2026, 9, 21)  # 첫 회차 (target_date)
D3 = date(2026, 9, 23)  # 둘째 회차


def _at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=KST)


# ── 라우트: 어젠다 표시 ─────────────────────────────────────────────────


def test_agenda_shows_split_card_on_its_second_session_day(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> None:
    """target_date 는 그저께지만 오늘 세션 블록이 있는 카드 — 오늘 카드로 뜬다(이월 아님)."""
    today = now_kst().date()
    card = ActionItem(
        id=uuid4(),
        user_id=DEMO_USER_UUID,
        title="운영체제 정리",
        target_date=today - timedelta(days=2),
        category="study",
        source="goal",
        status="partial_done",
        priority=3,
        estimated_minutes=90,
        archived_at=None,
    )
    fake_action_item_repo.seed(card)
    block = ScheduledBlock(
        id=uuid4(),
        user_id=DEMO_USER_UUID,
        action_item_id=card.id,
        start_at=datetime.combine(today, time(8, 0), tzinfo=KST),
        end_at=datetime.combine(today, time(8, 45), tzinfo=KST),
        block_status="scheduled",
        source="ai_plan",
    )
    fake_scheduled_block_repo.seed(block, title=card.title, category=card.category)

    resp = client.get("/today/agenda")

    assert resp.status_code == 200, resp.text
    cards = resp.json()["cards"]
    assert [c["actionId"] for c in cards] == [f"action_{card.id}"]
    assert cards[0]["carriedOver"] is False


# ── 실 Postgres ─────────────────────────────────────────────────────────


pytestmark_db = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")


async def _user(s: AsyncSession, *, onboarding: str = "ACTIVE") -> UUID:
    uid = uuid.uuid4()
    s.add(User(id=uid, email=f"split+{uid}@test.local", name="split", onboarding_state=onboarding))
    await s.flush()
    return uid


async def _card(
    s: AsyncSession,
    user_id: UUID,
    *,
    title: str,
    target: date = D1,
    status: str = "planned",
    archived: bool = False,
) -> UUID:
    card = ActionItem(
        id=uuid.uuid4(),
        user_id=user_id,
        title=title,
        target_date=target,
        category="study",
        source="goal",
        status=status,
        estimated_minutes=90,
        archived_at=now_kst() if archived else None,
    )
    s.add(card)
    await s.flush()
    return card.id


async def _block(
    s: AsyncSession,
    user_id: UUID,
    card_id: UUID,
    *,
    start: datetime,
    status: str = "scheduled",
    source: str = "ai_plan",
) -> UUID:
    b = ScheduledBlock(
        id=uuid.uuid4(),
        user_id=user_id,
        action_item_id=card_id,
        start_at=start,
        end_at=start + timedelta(minutes=45),
        block_status=status,
        source=source,
    )
    s.add(b)
    await s.flush()
    return b.id


async def _block_status(s: AsyncSession, block_id: UUID) -> str:
    stmt = select(ScheduledBlock.block_status).where(ScheduledBlock.id == block_id)
    return (await s.execute(stmt)).scalar_one()


@pytestmark_db
async def test_list_by_date_includes_cards_with_a_session_that_day(
    real_db_session: AsyncSession,
) -> None:
    s = real_db_session
    uid = await _user(s)
    other = await _user(s)

    split = await _card(s, uid, title="쪼갠 카드")
    await _block(s, uid, split, start=_at(D1, 8), status="finished")
    await _block(s, uid, split, start=_at(D3, 8))
    after_midnight = await _card(s, uid, title="0시 10분 회차")
    await _block(s, uid, after_midnight, start=_at(D3, 0, 10))
    dated_today = await _card(s, uid, title="원래 오늘 카드", target=D3)
    finished_today = await _card(s, uid, title="오늘 회차를 끝낸 카드", status="partial_done")
    await _block(s, uid, finished_today, start=_at(D3, 9), status="finished")

    # ── 아래는 전부 D3 어젠다에서 빠져야 한다 ──
    cancelled = await _card(s, uid, title="회차가 취소됨")
    await _block(s, uid, cancelled, start=_at(D3, 8), status="cancelled")
    night_before = await _card(s, uid, title="전날 23:50 회차")  # KST 로 잘라야 빠진다
    await _block(s, uid, night_before, start=_at(D3, 0) - timedelta(minutes=10))
    archived = await _card(s, uid, title="보관된 카드", archived=True)
    await _block(s, uid, archived, start=_at(D3, 8))
    others = await _card(s, other, title="남의 카드")
    await _block(s, other, others, start=_at(D3, 8))

    found = await ActionItemRepo(s).list_by_date(uid, D3)

    ids = [a.id for a in found]
    assert len(ids) == len(set(ids)), "블록이 여럿이어도 카드는 한 번만"
    assert set(ids) == {split, after_midnight, dated_today, finished_today}, [
        a.title for a in found
    ]
    assert not set(ids) & {cancelled, night_before, archived, others}

    # target_date 규칙은 그대로 — 첫날엔 원래대로 뜬다.
    assert split in {a.id for a in await ActionItemRepo(s).list_by_date(uid, D1)}


async def _running_session(s: AsyncSession, uid: UUID) -> tuple[ExecutionEvent, dict[str, UUID]]:
    """D1 회차를 진행 중인 쪼갠 카드 + 다른 회차들. 반환: (실행, 이름→블록 id)."""
    card = await _card(s, uid, title="쪼갠 카드", status="in_progress")
    blocks = {
        "earlier_finished": await _block(s, uid, card, start=_at(D1, 7), status="finished"),
        "current": await _block(s, uid, card, start=_at(D1, 8), status="started"),
        "later": await _block(s, uid, card, start=_at(D3, 8)),
        "moved_by_user": await _block(s, uid, card, start=_at(D3, 20), source="user_edit"),
    }
    other_card = await _card(s, uid, title="다른 카드")
    blocks["other_card"] = await _block(s, uid, other_card, start=_at(D3, 9))
    execution = ExecutionEvent(
        user_id=uid,
        action_item_id=card,
        scheduled_block_id=blocks["current"],
        plan_start_at=_at(D1, 8),
        plan_end_at=_at(D1, 8, 45),
        actual_start_at=_at(D1, 8),
        completion_status="in_progress",
    )
    s.add(execution)
    await s.flush()
    return execution, blocks


@pytestmark_db
@pytest.mark.parametrize("status", ["done", "over_done"])
async def test_finishing_the_card_cancels_its_remaining_sessions(
    real_db_session: AsyncSession, status: str
) -> None:
    s = real_db_session
    uid = await _user(s)
    execution, blocks = await _running_session(s, uid)

    await ExecutionRepo(s).close_execution(execution, status=status, ended_at=_at(D1, 8, 50))
    await s.flush()

    assert await _block_status(s, blocks["current"]) == "finished"
    assert await _block_status(s, blocks["later"]) == "cancelled"
    # 데이터 보호 — 수행 이력·사용자가 옮긴 블록·다른 카드는 그대로
    assert await _block_status(s, blocks["earlier_finished"]) == "finished"
    assert await _block_status(s, blocks["moved_by_user"]) == "scheduled"
    assert await _block_status(s, blocks["other_card"]) == "scheduled"


@pytestmark_db
@pytest.mark.parametrize("status", ["partial_done", "failed"])
async def test_unfinished_check_in_keeps_the_next_session(
    real_db_session: AsyncSession, status: str
) -> None:
    """'조금 함/못 함' 은 아직 남았다는 뜻 — 다음 회차가 이어서 할 자리다."""
    s = real_db_session
    uid = await _user(s)
    execution, blocks = await _running_session(s, uid)
    repo = ExecutionRepo(s)

    await repo.close_execution(execution, status=status, ended_at=_at(D1, 8, 50))
    await s.flush()

    assert await _block_status(s, blocks["current"]) == "finished"
    assert await _block_status(s, blocks["later"]) == "scheduled"
    # 다음 [▶ 시작] 은 남은 회차 블록을 잡는다(끝낸 블록은 다시 쓰지 않는다).
    nxt = await repo.find_open_block(uid, execution.action_item_id)
    assert nxt is not None and nxt.id == blocks["later"]


@pytestmark_db
async def test_pre_card_skips_blocks_of_finished_cards(real_db_session: AsyncSession) -> None:
    """이미 끝낸 카드의 남은 블록엔 '곧 시작' 이 가지 않는다 (체크인 정리의 이중 방어)."""
    s = real_db_session
    uid = await _user(s)
    start = _at(D3, 8)
    expected = {}
    for card_status in ("planned", "partial_done", "failed", "done", "over_done"):
        card = await _card(s, uid, title=f"{card_status} 카드", status=card_status)
        expected[await _block(s, uid, card, start=start)] = card_status

    found = await ExecutionRepo(s).list_blocks_starting_between(
        start=start - timedelta(minutes=1), end=start + timedelta(minutes=1)
    )

    statuses = sorted(expected[b.id] for b in found if b.id in expected)
    assert statuses == ["failed", "partial_done", "planned"]
