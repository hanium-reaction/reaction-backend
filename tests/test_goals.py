"""Goals 스텁 (api-contract §6 / #3-D)."""

from fastapi.testclient import TestClient

from reaction_backend.api.mock.goals import DEMO_GOALS


def test_list_goals_returns_by_tier(client: TestClient) -> None:
    resp = client.get("/goals")
    assert resp.status_code == 200
    body = resp.json()
    assert {"focus", "maintain", "parked"} <= set(body)
    assert isinstance(body["focus"], list)


def test_create_goal(client: TestClient) -> None:
    resp = client.post(
        "/goals",
        json={
            "title": "새 목표",
            "category": "학업",
            "goalTier": "focus",
            "priorityLevel": 1,
            "deadline": "2026-08-01",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["goalTier"] == "focus"
    assert body["status"] == "active"


def test_create_goal_rejects_bad_tier(client: TestClient) -> None:
    resp = client.post(
        "/goals",
        json={"title": "x", "category": "y", "goalTier": "bogus", "priorityLevel": 1},
    )
    assert resp.status_code == 422


def test_update_goal(client: TestClient) -> None:
    resp = client.patch(f"/goals/{DEMO_GOALS[0].goal_id}", json={"title": "수정된 제목"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "수정된 제목"


def test_update_goal_not_found(client: TestClient) -> None:
    resp = client.patch("/goals/nonexistent", json={"title": "x"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "GOAL_NOT_FOUND"


def test_decompose_goal(client: TestClient) -> None:
    resp = client.post(f"/goals/{DEMO_GOALS[0].goal_id}/decompose")
    assert resp.status_code == 200
    body = resp.json()
    assert body["goalId"] == DEMO_GOALS[0].goal_id
    assert body["rootNodeId"]
    assert isinstance(body["nodes"], list) and len(body["nodes"]) > 0


def test_park_goal(client: TestClient) -> None:
    resp = client.post(f"/goals/{DEMO_GOALS[0].goal_id}/park")
    assert resp.status_code == 200
    assert resp.json()["goalTier"] == "parked"


def test_delete_goal(client: TestClient) -> None:
    resp = client.delete(f"/goals/{DEMO_GOALS[0].goal_id}")
    assert resp.status_code == 204
