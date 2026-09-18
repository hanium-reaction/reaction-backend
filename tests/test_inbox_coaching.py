"""Inbox 개인화 조언 API (#399)."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.schemas.common import now_kst


def _action(*, user_id: Any, title: str, target_date: Any) -> ActionItem:
    action = ActionItem()
    action.id = uuid4()
    action.user_id = user_id
    action.title = title
    action.target_date = target_date
    action.status = "planned"
    action.priority = 1
    action.estimated_minutes = 30
    action.archived_at = None
    return action


def test_coaching_advice_is_empty_without_user_context(client: TestClient) -> None:
    response = client.get("/inbox/coaching-advice")
    assert response.status_code == 200
    assert response.json() == []


def test_coaching_advice_uses_only_current_users_actions(
    client: TestClient, fake_action_item_repo: Any, demo_user_orm: Any
) -> None:
    today = now_kst().date()
    mine = _action(user_id=demo_user_orm.id, title="발표 자료 마무리", target_date=today)
    mine.estimated_minutes = 45
    fake_action_item_repo.seed(mine)
    fake_action_item_repo.seed(
        _action(
            user_id=uuid4(), title="다른 사용자의 비밀 일정", target_date=today - timedelta(days=1)
        )
    )

    response = client.get("/inbox/coaching-advice")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["category"] == "today"
    assert body[0]["evidence"] == ["오늘 예정 1건", "예상 45분"]
    assert "다른 사용자의 비밀 일정" not in str(body)
    assert body[0]["action"]["type"] == "OPEN_TODAY"


# ── 마지막 안내는 지금 하기로 한 목표에서만 (inbox-10) ──────────────────────


def _goal(**kw: Any) -> Any:
    from reaction_backend.db.models.goal import Goal

    g = Goal()
    g.id = uuid4()
    g.title = kw.get("title", "토익 단어 외우기")
    g.status = kw.get("status", "active")
    g.goal_tier = kw.get("goal_tier", "focus")
    g.is_ultimate = kw.get("is_ultimate", False)
    g.priority_level = kw.get("priority_level", 1)
    g.deadline = None
    return g


def _advice(goals: list[Any]) -> list[Any]:
    from reaction_backend.orchestrator.inbox_coaching import build_coaching_advice

    now = now_kst()
    return build_coaching_advice(
        goals=goals,
        habits=[],
        today_actions=[],
        yesterday_actions=[],
        today=now.date(),
        generated_at=now,
    )


def test_fallback_does_not_name_a_parked_completed_proposed_or_ultimate_goal() -> None:
    for goal in (
        _goal(goal_tier="parked"),
        _goal(status="completed"),
        _goal(status="proposed"),
        _goal(is_ultimate=True, goal_tier="maintain"),
    ):
        assert _advice([goal]) == [], goal.__dict__


def test_fallback_names_an_active_goal_without_depending_on_batchim() -> None:
    [item] = _advice([_goal(goal_tier="maintain", title="코딩")])
    assert "‘코딩’" in item.title
    assert "이에요" not in item.title


def test_recovery_body_does_not_depend_on_batchim() -> None:
    from reaction_backend.orchestrator.inbox_coaching import build_coaching_advice

    now = now_kst()
    [item] = build_coaching_advice(
        goals=[],
        habits=[],
        today_actions=[],
        yesterday_actions=[_action(user_id=uuid4(), title="보고서", target_date=now.date())],
        today=now.date(),
        generated_at=now,
    )
    assert "‘보고서’ 등" in item.body
    assert "’을" not in item.body
