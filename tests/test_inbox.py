"""Inbox 스텁 (api-contract §18 / #3-D)."""

from fastapi.testclient import TestClient

from reaction_backend.api.mock.inbox import DEMO_INBOX_ITEMS


def test_list_inbox(client: TestClient) -> None:
    resp = client.get("/inbox")
    assert resp.status_code == 200
    assert len(resp.json()) == len(DEMO_INBOX_ITEMS)


def test_list_inbox_with_status_filter(client: TestClient) -> None:
    resp = client.get("/inbox", params={"status": "captured"})
    assert resp.status_code == 200
    assert all(item["status"] == "captured" for item in resp.json())


def test_create_inbox(client: TestClient) -> None:
    resp = client.post("/inbox", json={"rawText": "테스트 캡처"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["rawText"] == "테스트 캡처"
    assert body["status"] == "captured"


def test_create_inbox_rejects_empty(client: TestClient) -> None:
    resp = client.post("/inbox", json={"rawText": ""})
    assert resp.status_code == 422


def test_update_inbox(client: TestClient) -> None:
    resp = client.patch(f"/inbox/{DEMO_INBOX_ITEMS[0].inbox_id}", json={"userCategory": "프로젝트"})
    assert resp.status_code == 200
    assert resp.json()["userCategory"] == "프로젝트"


def test_update_inbox_not_found(client: TestClient) -> None:
    resp = client.patch("/inbox/nonexistent", json={"userCategory": "x"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "INBOX_NOT_FOUND"


def test_promote_to_goal(client: TestClient) -> None:
    resp = client.post(f"/inbox/{DEMO_INBOX_ITEMS[0].inbox_id}/promote")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "promoted"
    assert body["promotedGoalId"]


def test_delete_inbox(client: TestClient) -> None:
    resp = client.delete(f"/inbox/{DEMO_INBOX_ITEMS[0].inbox_id}")
    assert resp.status_code == 204
