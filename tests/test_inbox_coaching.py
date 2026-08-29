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
