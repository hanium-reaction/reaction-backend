"""주간 그리드가 끝낸 블록의 완료·실패를 구분한다 (planA-10 / journey-1).

체크인은 결과와 무관하게 블록을 `finished` 로 닫는다 — `blockStatus` 만으로는 끝냈는지 못
했는지 알 수 없어, 주간 캘린더의 '완료 N' 이 늘 0 이고 실패한 회차에 ✗ 도 없었다. 블록마다
마지막 체크인 결과를 `completionStatus` 로 싣는다(카드 단위가 아니다 — 회차마다 다르다).
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.repositories.scheduled_block_repo import ScheduledBlockRepo
from reaction_backend.schemas.common import now_kst
from tests.conftest import DB_AVAILABLE, FakeScheduledBlockRepo
from tests.test_planning_weekly import MON, _block, _dt


def test_weekly_tells_done_from_failed_for_finished_blocks(
    client: TestClient, fake_scheduled_block_repo: FakeScheduledBlockRepo
) -> None:
    rows = {
        "알고리즘": ("finished", "done"),
        "동아리": ("finished", "partial_done"),
        "SQL": ("finished", "failed"),
        "진행 중": ("started", "in_progress"),
        "예정": ("scheduled", None),
    }
    for i, (title, (status, outcome)) in enumerate(rows.items()):
        block = _block(_dt(1, 8 + 2 * i, 0), _dt(1, 9 + 2 * i, 0), status=status)
        fake_scheduled_block_repo.seed(block, title=title, category="study")
        if outcome is not None:
            fake_scheduled_block_repo._completion[block.id] = outcome

    resp = client.get("/plans/weekly", params={"weekStart": MON.isoformat()})

    assert resp.status_code == 200
    got = {
        b["title"]: (b["blockStatus"], b["completionStatus"])
        for b in resp.json()["days"][1]["blocks"]
    }
    assert got == {
        "알고리즘": ("finished", "done"),
        "동아리": ("finished", "partial_done"),
        "SQL": ("finished", "failed"),
        "진행 중": ("started", None),
        "예정": ("scheduled", None),
    }


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")
async def test_completion_by_block_reads_the_latest_finished_checkin(
    real_db_session: AsyncSession,
) -> None:
    """실 SQL — 블록 단위, 진행 중 제외, 한 블록에 기록이 여럿이면 가장 늦은 것, 남의 기록 제외."""
    db = real_db_session
    user = User(id=uuid4(), email=f"{uuid4()}@test.local", name="주간 완료 표시")
    other = User(id=uuid4(), email=f"{uuid4()}@test.local", name="다른 사람")
    db.add_all([user, other])
    await db.flush()
    action = ActionItem(
        id=uuid4(),
        user_id=user.id,
        title="분할 카드",
        target_date=now_kst().date(),
        estimated_minutes=120,
        category="study",
    )
    db.add(action)
    await db.flush()
    start = now_kst().replace(microsecond=0)
    blocks = [
        ScheduledBlock(
            id=uuid4(),
            user_id=user.id,
            action_item_id=action.id,
            start_at=start + timedelta(hours=i),
            end_at=start + timedelta(hours=i, minutes=50),
            block_status="finished",
        )
        for i in range(3)
    ]
    db.add_all(blocks)
    await db.flush()

    def _event(block: ScheduledBlock, status: str, ended_min: int | None) -> ExecutionEvent:
        return ExecutionEvent(
            id=uuid4(),
            user_id=user.id,
            action_item_id=action.id,
            scheduled_block_id=block.id,
            plan_start_at=block.start_at,
            plan_end_at=block.end_at,
            actual_end_at=None
            if ended_min is None
            else block.start_at + timedelta(minutes=ended_min),
            completion_status=status,
        )

    db.add_all(
        [
            _event(blocks[0], "failed", 10),
            _event(blocks[0], "done", 40),  # 같은 회차를 다시 해서 끝냄 — 늦은 것이 이긴다
            _event(blocks[1], "failed", 30),
            _event(blocks[2], "in_progress", None),  # 진행 중은 결과가 아니다
        ]
    )
    await db.flush()

    repo = ScheduledBlockRepo(db)
    got = await repo.completion_by_block(user.id, [b.id for b in blocks])
    assert got == {blocks[0].id: "done", blocks[1].id: "failed"}
    assert await repo.completion_by_block(other.id, [b.id for b in blocks]) == {}
    assert await repo.completion_by_block(user.id, []) == {}
