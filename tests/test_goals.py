"""Goals — 실 구현 (Issue #22, api-contract §6)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


def _new_goal(client: TestClient, *, title: str = "캡스톤", tier: str = "focus") -> dict[str, Any]:
    resp = client.post(
        "/goals",
        json={
            "title": title,
            "category": "project",
            "goalTier": tier,
            "priorityLevel": 1,
        },
    )
    assert resp.status_code == 201, resp.json()
    return resp.json()


def test_list_empty(client: TestClient) -> None:
    resp = client.get("/goals")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"focus": [], "maintain": [], "parked": []}


def test_create_goal(client: TestClient) -> None:
    body = _new_goal(client)
    assert body["title"] == "캡스톤"
    assert body["goalTier"] == "focus"
    assert body["goalId"].startswith("goal_")
    assert body["status"] == "active"


def test_create_rejects_bad_category(client: TestClient) -> None:
    resp = client.post(
        "/goals",
        json={
            "title": "x",
            "category": "bogus_cat",
            "goalTier": "focus",
            "priorityLevel": 1,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"
    assert resp.json()["field"] == "category"


def test_create_rejects_bad_tier(client: TestClient) -> None:
    resp = client.post(
        "/goals",
        json={
            "title": "x",
            "category": "study",
            "goalTier": "bogus",
            "priorityLevel": 1,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_focus_tier_limit_3(client: TestClient) -> None:
    """Focus 4번째 → 422 GOAL_TIER_LIMIT_EXCEEDED."""
    for i in range(3):
        _new_goal(client, title=f"g{i}", tier="focus")
    resp = client.post(
        "/goals",
        json={
            "title": "over",
            "category": "study",
            "goalTier": "focus",
            "priorityLevel": 1,
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "GOAL_TIER_LIMIT_EXCEEDED"
    assert body["field"] == "goalTier"


def test_maintain_tier_limit_5(client: TestClient) -> None:
    for i in range(5):
        _new_goal(client, title=f"m{i}", tier="maintain")
    resp = client.post(
        "/goals",
        json={
            "title": "over",
            "category": "study",
            "goalTier": "maintain",
            "priorityLevel": 3,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "GOAL_TIER_LIMIT_EXCEEDED"


def test_parked_has_no_limit(client: TestClient) -> None:
    """Parked 자유 — 한도 X (DevBaseline §1.4)."""
    for i in range(10):
        _new_goal(client, title=f"p{i}", tier="parked")
    items = client.get("/goals").json()
    assert len(items["parked"]) == 10


def test_list_groups_by_tier(client: TestClient) -> None:
    _new_goal(client, title="f1", tier="focus")
    _new_goal(client, title="m1", tier="maintain")
    _new_goal(client, title="p1", tier="parked")
    body = client.get("/goals").json()
    assert len(body["focus"]) == 1
    assert len(body["maintain"]) == 1
    assert len(body["parked"]) == 1


def test_update_title(client: TestClient) -> None:
    created = _new_goal(client)
    resp = client.patch(f"/goals/{created['goalId']}", json={"title": "수정"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "수정"


def test_update_tier_focus_to_parked(client: TestClient) -> None:
    created = _new_goal(client, tier="focus")
    resp = client.patch(f"/goals/{created['goalId']}", json={"goalTier": "parked"})
    assert resp.status_code == 200
    assert resp.json()["goalTier"] == "parked"


def test_update_tier_rejects_over_limit(client: TestClient) -> None:
    """Focus 3 + Maintain 1 → Maintain 을 Focus 로 변경 시도 → 422."""
    for i in range(3):
        _new_goal(client, title=f"f{i}", tier="focus")
    target = _new_goal(client, title="m", tier="maintain")
    resp = client.patch(f"/goals/{target['goalId']}", json={"goalTier": "focus"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "GOAL_TIER_LIMIT_EXCEEDED"


def test_update_goal_not_found(client: TestClient) -> None:
    resp = client.patch(
        "/goals/goal_99999999-9999-4999-8999-999999999999",
        json={"title": "x"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "GOAL_NOT_FOUND"


def test_update_bad_id_format(client: TestClient) -> None:
    resp = client.patch("/goals/nonexistent", json={"title": "x"})
    assert resp.status_code == 404


def test_update_bad_deadline_format(client: TestClient) -> None:
    created = _new_goal(client)
    resp = client.patch(f"/goals/{created['goalId']}", json={"deadline": "not-a-date"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_decompose_returns_stub(client: TestClient) -> None:
    """본 PR 은 mock 룰 stub — LLM 통합은 후속 PR."""
    created = _new_goal(client)
    resp = client.post(f"/goals/{created['goalId']}/decompose")
    assert resp.status_code == 200
    body = resp.json()
    assert body["goalId"] == created["goalId"]
    assert body["nodes"]
    assert body["rootNodeId"]


def test_park_focus_to_parked(client: TestClient) -> None:
    created = _new_goal(client, tier="focus")
    resp = client.post(f"/goals/{created['goalId']}/park")
    assert resp.status_code == 200
    assert resp.json()["goalTier"] == "parked"


def test_park_not_found(client: TestClient) -> None:
    resp = client.post("/goals/goal_99999999-9999-4999-8999-999999999999/park")
    assert resp.status_code == 404


def test_delete_goal(client: TestClient) -> None:
    created = _new_goal(client)
    resp = client.delete(f"/goals/{created['goalId']}")
    assert resp.status_code == 204
    body = client.get("/goals").json()
    assert body == {"focus": [], "maintain": [], "parked": []}
