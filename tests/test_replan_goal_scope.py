"""재계획이 목표의 상태·마감을 본다.

- planA-8: 지운(보관)·완료한 목표의 카드는 '남은 일' 로 다시 배치하지 않는다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.goal import Goal
from tests.conftest import (
    DEMO_USER_UUID,
    FakeActionItemRepo,
    FakeGoalRepo,
    FakeScheduledBlockRepo,
)
from tests.test_replan_route import _freeze_now, _kst, _seed_action, _seed_block


def _seed_goal(
    repo: FakeGoalRepo,
    *,
    title: str,
    status: str = "active",
    deadline: date | None = None,
    archived: bool = False,
) -> Goal:
    g = Goal()
    g.id = uuid4()
    g.user_id = DEMO_USER_UUID
    g.title = title
    g.category = "study"
    g.goal_tier = "focus"
    g.status = status
    g.deadline = deadline
    g.archived_at = datetime.now(UTC) if archived else None
    repo._items[g.id] = g
    return g


def _card(repo: FakeActionItemRepo, goal: Goal, *, title: str, est: int = 60) -> ActionItem:
    a = _seed_action(repo, title=title, est=est)
    a.goal_id = goal.id
    return a


def _action_ids(resp: Any) -> set[str]:
    return {b["actionId"] for b in resp.json()["blocks"]}


def test_replan_leaves_out_cards_of_a_deleted_or_completed_goal(
    monkeypatch: Any,
    client: TestClient,
    fake_goal_repo: FakeGoalRepo,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> None:
    _freeze_now(monkeypatch)
    live = _seed_goal(fake_goal_repo, title="토익 900")
    deleted = _seed_goal(fake_goal_repo, title="그만둔 목표", status="archived", archived=True)
    done = _seed_goal(fake_goal_repo, title="끝낸 목표", status="completed")

    live_card = _card(fake_action_item_repo, live, title="RC 파트5")
    deleted_card = _card(fake_action_item_repo, deleted, title="지운 목표 카드")
    done_card = _card(fake_action_item_repo, done, title="끝낸 목표 카드")
    inbox_card = _seed_action(fake_action_item_repo, title="인박스 할 일", target=date(2026, 7, 1))
    for i, card in enumerate((live_card, deleted_card, done_card)):
        _seed_block(
            fake_scheduled_block_repo,
            action_id=card.id,
            start=_kst(2026, 7, 14 + i, 10, 0),
            end=_kst(2026, 7, 14 + i, 11, 0),
        )

    resp = client.post("/plans/replan")

    assert resp.status_code == 201, resp.text
    ids = _action_ids(resp)
    assert f"action_{live_card.id}" in ids
    assert f"action_{inbox_card.id}" in ids  # 목표 없는 카드는 그대로 후보
    assert f"action_{deleted_card.id}" not in ids
    assert f"action_{done_card.id}" not in ids
