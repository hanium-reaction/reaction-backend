"""Inbox — 실 구현 (Issue #22-B, api-contract §18).

`GEMINI_API_KEY` 가 빈 상태이므로 `aiClient.run` 은 자동으로 룰 fallback 분기.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


def _capture(client: TestClient, text: str = "캡스톤 설계 단계 정리") -> dict[str, Any]:
    resp = client.post("/inbox", json={"rawText": text})
    assert resp.status_code == 201, resp.json()
    return resp.json()


def test_list_empty(client: TestClient) -> None:
    resp = client.get("/inbox")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_captures_and_classifies(client: TestClient) -> None:
    body = _capture(client, "캡스톤 설계 단계 정리")
    assert body["inboxId"].startswith("inbox_")
    assert body["rawText"] == "캡스톤 설계 단계 정리"
    # 룰 fallback 키워드 "캡스톤" → project
    assert body["aiCategoryGuess"] == "project"
    assert body["status"] == "classified"
    assert body["userCategory"] is None
    assert body["promotedGoalId"] is None


def test_create_rule_fallback_other(client: TestClient) -> None:
    """매칭되는 키워드 없으면 ai_category_guess=other."""
    body = _capture(client, "흠 뭔가 생각났는데")
    assert body["aiCategoryGuess"] == "other"


def test_create_rejects_empty_text(client: TestClient) -> None:
    resp = client.post("/inbox", json={"rawText": ""})
    assert resp.status_code == 422


def test_create_encrypts_raw_text(client: TestClient, fake_inbox_repo: Any) -> None:
    """DB에 저장되는 raw_text 는 평문 X — 응답만 복호화."""
    _capture(client, "운동 매일 30분")
    # fake repo 내부의 raw_text_encrypted 가 원문과 다름 (암호화)
    stored = next(iter(fake_inbox_repo._items.values()))
    assert stored.raw_text_encrypted != "운동 매일 30분"


def test_list_after_create(client: TestClient) -> None:
    _capture(client, "토익 단어 외우기")
    items = client.get("/inbox").json()
    assert len(items) == 1
    assert items[0]["aiCategoryGuess"] == "study"


def test_list_filter_by_status(client: TestClient) -> None:
    _capture(client, "운동")
    _capture(client, "프로젝트")
    classified = client.get("/inbox", params={"status": "classified"}).json()
    archived = client.get("/inbox", params={"status": "archived"}).json()
    assert len(classified) == 2
    assert archived == []


def test_patch_user_category(client: TestClient) -> None:
    created = _capture(client)
    resp = client.patch(f"/inbox/{created['inboxId']}", json={"userCategory": "study"})
    assert resp.status_code == 200
    assert resp.json()["userCategory"] == "study"


def test_patch_rejects_bad_category(client: TestClient) -> None:
    created = _capture(client)
    resp = client.patch(f"/inbox/{created['inboxId']}", json={"userCategory": "bogus"})
    assert resp.status_code == 422


def test_patch_not_found(client: TestClient) -> None:
    resp = client.patch(
        "/inbox/inbox_99999999-9999-4999-8999-999999999999",
        json={"userCategory": "study"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "INBOX_NOT_FOUND"


def test_patch_bad_id_format(client: TestClient) -> None:
    resp = client.patch("/inbox/nonexistent", json={"userCategory": "study"})
    assert resp.status_code == 404


def test_convert_to_goal(client: TestClient, fake_goal_repo: Any) -> None:
    created = _capture(client, "캡스톤 마무리")
    resp = client.post(f"/inbox/{created['inboxId']}/convert-to-goal")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "promoted"
    assert body["promotedGoalId"] is not None
    assert body["promotedGoalId"].startswith("goal_")
    # Goal 실제 생성 확인
    goals = client.get("/goals").json()
    assert len(goals["maintain"]) == 1
    assert goals["maintain"][0]["title"] == "캡스톤 마무리"


def test_convert_to_goal_uses_user_category_override(client: TestClient) -> None:
    """user_category 가 있으면 우선 사용."""
    created = _capture(client, "어떤 텍스트")  # ai_category_guess=other
    client.patch(f"/inbox/{created['inboxId']}", json={"userCategory": "study"})
    client.post(f"/inbox/{created['inboxId']}/convert-to-goal")
    goal = client.get("/goals").json()["maintain"][0]
    assert goal["category"] == "study"


def test_convert_to_goal_rejects_over_maintain_limit(client: TestClient) -> None:
    """Maintain 한도 5 — 6번째 convert 시 422."""
    for i in range(5):
        client.post(
            "/goals",
            json={
                "title": f"m{i}",
                "category": "study",
                "goalTier": "maintain",
                "priorityLevel": 3,
            },
        )
    created = _capture(client, "over")
    resp = client.post(f"/inbox/{created['inboxId']}/convert-to-goal")
    assert resp.status_code == 422
    assert resp.json()["code"] == "GOAL_TIER_LIMIT_EXCEEDED"


def test_convert_to_goal_not_found(client: TestClient) -> None:
    resp = client.post("/inbox/inbox_99999999-9999-4999-8999-999999999999/convert-to-goal")
    assert resp.status_code == 404


def test_convert_to_action(client: TestClient, fake_action_item_repo: Any) -> None:
    created = _capture(client, "오늘 산책")
    resp = client.post(f"/inbox/{created['inboxId']}/convert-to-action")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "promoted"
    assert body["promotedGoalId"] is None  # action 변환은 goal 미연결
    # ActionItem 실제 생성 확인
    actions = list(fake_action_item_repo._items.values())
    assert len(actions) == 1
    assert actions[0].title == "오늘 산책"
    assert actions[0].source == "inbox"
    assert actions[0].inbox_item_id is not None


def test_convert_to_action_not_found(client: TestClient) -> None:
    resp = client.post("/inbox/inbox_99999999-9999-4999-8999-999999999999/convert-to-action")
    assert resp.status_code == 404


def test_archive(client: TestClient) -> None:
    created = _capture(client)
    resp = client.post(f"/inbox/{created['inboxId']}/archive")
    assert resp.status_code == 204
    assert client.get("/inbox").json() == []


def test_archive_not_found(client: TestClient) -> None:
    resp = client.post("/inbox/inbox_99999999-9999-4999-8999-999999999999/archive")
    assert resp.status_code == 404


def test_archive_bad_id_format(client: TestClient) -> None:
    resp = client.post("/inbox/nonexistent/archive")
    assert resp.status_code == 404
