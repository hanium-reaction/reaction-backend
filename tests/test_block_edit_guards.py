"""주간 캘린더 블록 편집(PATCH /plans/{planId}/blocks/{blockId})의 가드.

- planA-9: 이미 시작했거나 끝낸 블록은 시간을 옮길 수 없다(제목·목표만 바꾸는 건 허용).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeActionItemRepo, FakeScheduledBlockRepo
from tests.test_planning_weekly import MON, _action, _block, _dt, _patch


@pytest.mark.parametrize("status", ["started", "finished"])
def test_a_started_or_finished_block_cannot_be_moved(
    status: str,
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """끝낸 블록을 다음 주로 끌면 수행 기록이 미래로 가고 카드가 오늘 화면에서 사라졌다."""
    action = _action()
    fake_action_item_repo.seed(action)
    block = _block(_dt(1, 9, 0), _dt(1, 10, 0), action_id=action.id, status=status)
    fake_scheduled_block_repo.seed(block, title=action.title, category=action.category)

    resp: Any = _patch(client, f"block_{block.id}", {"startAt": _dt(4, 15, 0).isoformat()})

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "PLAN_INVALID_TIME"
    assert "옮길 수 없어요" in body["message"]
    assert (block.start_at, block.end_at, block.source) == (_dt(1, 9, 0), _dt(1, 10, 0), "ai_plan")
    assert action.target_date == MON


def test_a_finished_block_can_still_be_renamed(
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """시각을 그대로 보내고 제목만 바꾸면 통과 — 시각·출처는 건드리지 않는다."""
    action = _action()
    fake_action_item_repo.seed(action)
    block = _block(_dt(1, 9, 0), _dt(1, 10, 0), action_id=action.id, status="finished")
    fake_scheduled_block_repo.seed(block, title=action.title, category=action.category)
    # 끝낸 블록과 겹치는 다른 블록이 있어도 제목 편집은 막지 않는다(시간을 안 옮기니까).
    fake_scheduled_block_repo.seed(
        _block(_dt(1, 9, 30), _dt(1, 10, 30)), title="겹친 일정", category="study"
    )

    resp: Any = _patch(
        client,
        f"block_{block.id}",
        {"startAt": _dt(1, 9, 0).isoformat(), "title": "SQL 복습 끝"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "SQL 복습 끝"
    assert body["blockStatus"] == "finished"
    assert body["source"] == "ai_plan"
    assert body["startAt"].startswith(f"{_dt(1, 9, 0).date().isoformat()}T09:00")
