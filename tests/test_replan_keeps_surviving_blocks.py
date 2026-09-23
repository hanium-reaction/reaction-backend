"""재계획이 '남겨 두는' 예정 블록 위에 새 블록을 겹쳐 잡지 않는다 (planA-7).

후보 루프는 형제 세션을 착수한 카드·사용자가 옮긴 카드를 통째 보존한다 — 그 카드의 나머지
예정 회차도 그대로 남는다. 그런데 회피 대상(busy)은 '확정'(시작/완료·user_edit) 블록만
넣어서, 남은 예정 회차 위에 다른 카드가 겹쳐 잡혔다. 승인하면 캘린더에 같은 시간 블록이
두 개 생긴다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import FakeActionItemRepo, FakeScheduledBlockRepo
from tests.test_replan_route import _freeze_now, _kst, _seed_action, _seed_block

# 2026-07-13(월) 08:00 — 기본 수면창(23~08) 직후라 스케줄러가 가장 먼저 고르는 자리다.
_MON_8 = _kst(2026, 7, 13, 8, 0)
_MON_9 = _kst(2026, 7, 13, 9, 0)


def _overlapping(resp: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    return [
        b
        for b in resp.json()["blocks"]
        if datetime.fromisoformat(b["start"]) < end and datetime.fromisoformat(b["end"]) > start
    ]


def test_replan_does_not_stack_on_a_partly_done_cards_remaining_session(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> None:
    """120분 카드의 1회차를 끝냈다 → 2회차(다음 주 월 08시)는 남는다. 다른 카드가 그 위에 오면 안 된다."""
    _freeze_now(monkeypatch)
    card_a = _seed_action(fake_action_item_repo, title="보고서", est=120)
    _seed_block(
        fake_scheduled_block_repo,
        action_id=card_a.id,
        start=_kst(2026, 7, 8, 10, 0),
        end=_kst(2026, 7, 8, 11, 0),
        status="finished",
    )
    _seed_block(fake_scheduled_block_repo, action_id=card_a.id, start=_MON_8, end=_MON_9)
    card_b = _seed_action(fake_action_item_repo, title="단어 암기", est=60)
    _seed_block(
        fake_scheduled_block_repo,
        action_id=card_b.id,
        start=_kst(2026, 7, 14, 15, 0),
        end=_kst(2026, 7, 14, 16, 0),
    )

    resp = client.post("/plans/replan")

    assert resp.status_code == 201, resp.text
    assert any(b["actionId"] == f"action_{card_b.id}" for b in resp.json()["blocks"])
    assert _overlapping(resp, _MON_8, _MON_9) == []


def test_replan_does_not_stack_on_a_card_the_user_moved(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> None:
    """회차 하나를 사용자가 옮긴 카드는 통째 보존 — 나머지 AI 회차(월 08시)도 남으니 피해야 한다."""
    _freeze_now(monkeypatch)
    card_a = _seed_action(fake_action_item_repo, title="발표 준비", est=120)
    _seed_block(
        fake_scheduled_block_repo,
        action_id=card_a.id,
        start=_kst(2026, 7, 10, 19, 0),
        end=_kst(2026, 7, 10, 20, 0),
        source="user_edit",
    )
    _seed_block(fake_scheduled_block_repo, action_id=card_a.id, start=_MON_8, end=_MON_9)
    card_b = _seed_action(fake_action_item_repo, title="단어 암기", est=60)
    _seed_block(
        fake_scheduled_block_repo,
        action_id=card_b.id,
        start=_kst(2026, 7, 14, 15, 0),
        end=_kst(2026, 7, 14, 16, 0),
    )

    resp = client.post("/plans/replan")

    assert resp.status_code == 201, resp.text
    assert not [b for b in resp.json()["blocks"] if b["actionId"] == f"action_{card_a.id}"]
    assert _overlapping(resp, _MON_8, _MON_9) == []


def test_replan_can_reuse_the_slot_of_a_block_it_replaces(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> None:
    """교체될 옛 블록의 자리는 비워 준다 — 그 자리까지 막으면 재배치가 괜히 밀린다."""
    _freeze_now(monkeypatch)
    card = _seed_action(fake_action_item_repo, title="단어 암기", est=60)
    _seed_block(fake_scheduled_block_repo, action_id=card.id, start=_MON_8, end=_MON_9)

    resp = client.post("/plans/replan")

    assert resp.status_code == 201, resp.text
    starts = [datetime.fromisoformat(b["start"]) for b in resp.json()["blocks"]]
    assert _MON_8 in starts
