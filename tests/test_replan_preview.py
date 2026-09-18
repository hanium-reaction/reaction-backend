"""재계획 미리보기가 '기존 → 새' 시각을 보여 줄 수 있게 옛 블록 시각을 싣는다 (planA-15).

미리보기는 교체할 옛 블록의 id 만 실어서, FE 는 내부 id 를 그대로 노출하는 것 말고는
무엇이 어디로 옮겨지는지 보여 줄 방법이 없었다.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from tests.conftest import FakeActionItemRepo, FakePlanDraftRepo, FakeScheduledBlockRepo
from tests.test_replan_route import (
    _freeze_now,
    _kst,
    _pblock,
    _seed_action,
    _seed_block,
    _seed_replan_draft,
)


def test_replan_preview_carries_the_replaced_blocks_original_time(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> None:
    _freeze_now(monkeypatch)
    moved = _seed_action(fake_action_item_repo, title="RC 파트5", est=60)
    _seed_block(
        fake_scheduled_block_repo,
        action_id=moved.id,
        start=_kst(2026, 7, 14, 15, 0),
        end=_kst(2026, 7, 14, 16, 0),
    )
    backlog = _seed_action(fake_action_item_repo, title="밀린 단어", target=date(2026, 7, 1))

    resp = client.post("/plans/replan")

    assert resp.status_code == 201, resp.text
    by_action = {b["actionId"]: b for b in resp.json()["blocks"]}
    replaced = by_action[f"action_{moved.id}"]
    assert replaced["replacesStart"] == "2026-07-14T15:00:00+09:00"
    assert replaced["replacesEnd"] == "2026-07-14T16:00:00+09:00"
    fresh = by_action[f"action_{backlog.id}"]
    assert fresh["replacesBlockId"] is None
    assert fresh["replacesStart"] is None and fresh["replacesEnd"] is None


def test_a_draft_made_before_this_field_still_renders(
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """이 필드 이전에 저장된 초안(payload 에 replacesStart 없음)도 null 로 읽힌다."""
    from reaction_backend.api.routes.planning import _replan_response

    draft_id = _seed_replan_draft(
        fake_plan_draft_repo,
        blocks=[
            _pblock(
                action_id=uuid4(),
                start=_kst(2026, 7, 14, 8, 0),
                end=_kst(2026, 7, 14, 8, 30),
                replaces=uuid4(),
            )
        ],
    )
    draft = fake_plan_draft_repo._items[UUID(draft_id)]

    block = _replan_response(draft).blocks[0]

    assert block.replaces_block_id is not None
    assert block.replaces_start is None and block.replaces_end is None
