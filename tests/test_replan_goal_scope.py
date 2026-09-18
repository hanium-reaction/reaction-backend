"""재계획이 목표의 상태·마감을 본다.

- planA-8: 지운(보관)·완료한 목표의 카드는 '남은 일' 로 다시 배치하지 않는다.
- planA-6: 목표마다 자기 마감 안에 배치한다 — 지평 하나로 균등 분산하면 금요일 시험 목표의
  남은 세션이 한 달짜리 목표의 지평에 섞여 시험 뒤로 밀렸다. 마감이 이미 지난 목표는 배치하되
  그 사실을 알린다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
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


def _blocks_of(resp: Any, card_ids: set[str]) -> list[dict[str, Any]]:
    return [b for b in resp.json()["blocks"] if b["actionId"] in card_ids]


def test_replan_keeps_an_exam_goals_sessions_before_its_deadline(
    monkeypatch: Any,
    client: TestClient,
    fake_goal_repo: FakeGoalRepo,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> None:
    """시험(7/17 금) 목표 카드 4개 + 4주짜리 프로젝트 카드 12개 — 시험 카드는 전부 7/17 안에."""
    _freeze_now(monkeypatch)  # window_start = 2026-07-13(월)
    exam = _seed_goal(fake_goal_repo, title="토익 시험", deadline=date(2026, 7, 17))
    project = _seed_goal(fake_goal_repo, title="캡스톤", deadline=date(2026, 8, 9))
    for i in range(12):  # 프로젝트 블록이 8/9 까지 흩어져 있어 전체 지평이 8/9 가 된다
        card = _card(fake_action_item_repo, project, title=f"캡스톤 {i}")
        day = date(2026, 7, 13) + timedelta(days=i * 27 // 11)
        _seed_block(
            fake_scheduled_block_repo,
            action_id=card.id,
            start=_kst(day.year, day.month, day.day, 14, 0),
            end=_kst(day.year, day.month, day.day, 15, 0),
        )
    exam_ids = {
        f"action_{_card(fake_action_item_repo, exam, title=f'RC {i}').id}" for i in range(4)
    }

    resp = client.post("/plans/replan")

    assert resp.status_code == 201, resp.text
    exam_blocks = _blocks_of(resp, exam_ids)
    assert len(exam_blocks) == 4
    late = [b for b in exam_blocks if datetime.fromisoformat(b["end"]).date() > date(2026, 7, 17)]
    assert late == [], late
    # 프로젝트 카드는 여전히 자기 지평(8/9)까지 퍼진다 — 시험 마감으로 같이 당겨지지 않는다.
    project_days = [
        datetime.fromisoformat(b["start"]).date()
        for b in resp.json()["blocks"]
        if b["actionId"] not in exam_ids
    ]
    assert max(project_days) > date(2026, 7, 31)
    assert not any("마감(" in w for w in resp.json()["warnings"])


def test_replan_says_so_when_a_goal_cannot_fit_before_its_deadline(
    monkeypatch: Any,
    client: TestClient,
    fake_goal_repo: FakeGoalRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """마감(7/13) 하루에 20시간 분량 — 넘친 세션은 마감 뒤로 밀지 않고 한 줄로 알린다."""
    _freeze_now(monkeypatch)
    exam = _seed_goal(fake_goal_repo, title="중간고사", deadline=date(2026, 7, 13))
    ids = {
        f"action_{_card(fake_action_item_repo, exam, title=f'범위 {i}', est=240).id}"
        for i in range(5)
    }

    resp = client.post("/plans/replan")

    assert resp.status_code == 201, resp.text
    blocks = _blocks_of(resp, ids)
    assert blocks
    assert all(datetime.fromisoformat(b["start"]).date() == date(2026, 7, 13) for b in blocks)
    notes = [w for w in resp.json()["warnings"] if "'중간고사' 마감(7월 13일)" in w]
    assert len(notes) == 1, resp.json()["warnings"]
    # 세션마다 한 줄씩 늘어놓지 않는다 — 목표 단위 한 줄.
    assert not any("배치할 가용 시간을 찾지 못했어요" in w for w in resp.json()["warnings"])


def test_replan_says_so_when_a_goals_deadline_has_already_passed(
    monkeypatch: Any,
    client: TestClient,
    fake_goal_repo: FakeGoalRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """리뷰 반영: 마감(7/10)이 재배치 시작(7/13) 전에 이미 지난 목표의 카드는 버리지 않고
    배치하되, 마감 뒤에 잡았다는 걸 목표 단위 한 줄로 알린다(예전엔 아무 말이 없었다)."""
    _freeze_now(monkeypatch)
    exam = _seed_goal(fake_goal_repo, title="중간고사", deadline=date(2026, 7, 10))
    ids = {f"action_{_card(fake_action_item_repo, exam, title=f'범위 {i}').id}" for i in range(2)}

    resp = client.post("/plans/replan")

    assert resp.status_code == 201, resp.text
    assert len(_blocks_of(resp, ids)) == 2
    notes = [w for w in resp.json()["warnings"] if "'중간고사' 마감(7월 10일)이 이미 지나서" in w]
    assert len(notes) == 1, resp.json()["warnings"]


def test_replan_fills_placeholders_in_the_users_tone(
    monkeypatch: Any,
    client: TestClient,
    demo_user_orm: Any,
    fake_goal_repo: FakeGoalRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """planA-19 — 자리표시자 채우기(LLM)도 사용자가 고른 말투를 받는다."""
    from reaction_backend.orchestrator import continuation_fill
    from tests.test_replan_route import _seed_rule_node

    _freeze_now(monkeypatch)
    demo_user_orm.tone_mode = "strict"
    goal = _seed_goal(fake_goal_repo, title="정보처리기사", deadline=date(2026, 11, 30))
    node = _seed_rule_node(fake_goal_repo, goal_id=goal.id, title="목표 21회차")
    card = _card(fake_action_item_repo, goal, title="목표 21회차")
    card.goal_node_id = node.id
    seen: dict[str, Any] = {}

    async def capture(session: Any, **kwargs: Any) -> list[Any]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(continuation_fill, "fill_cards", capture)

    resp = client.post("/plans/replan")

    assert resp.status_code == 201, resp.text
    assert seen.get("tone_mode") == "strict"


def test_replan_leaves_this_weeks_blockless_cards_where_they_are(
    monkeypatch: Any,
    client: TestClient,
    fake_goal_repo: FakeGoalRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """planA-17 — 오늘(7/9 목) 인박스에서 '할 일로' 바꾼 카드는 다음 주로 끌려가지 않는다."""
    _freeze_now(monkeypatch)  # 오늘 7/9(목), window_start 7/13(월)
    today_card = _seed_action(fake_action_item_repo, title="우체국 들르기", target=date(2026, 7, 9))
    sunday_card = _seed_action(fake_action_item_repo, title="방 정리", target=date(2026, 7, 12))
    goal = _seed_goal(fake_goal_repo, title="토익 900")
    overdue = _card(fake_action_item_repo, goal, title="지난주 못 한 RC")
    overdue.target_date = date(2026, 7, 1)
    undated = _seed_action(fake_action_item_repo, title="날짜 없는 백로그")

    resp = client.post("/plans/replan")

    assert resp.status_code == 201, resp.text
    ids = _action_ids(resp)
    assert f"action_{overdue.id}" in ids
    assert f"action_{undated.id}" in ids
    assert f"action_{today_card.id}" not in ids
    assert f"action_{sunday_card.id}" not in ids
