"""Calendar 스텁 (api-contract §9 / #3-C)."""

from fastapi.testclient import TestClient


def test_connect(client: TestClient) -> None:
    resp = client.post("/calendar/connect", json={"code": "oauth-code"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["connected"] is True
    assert body["provider"] == "google"


def test_connect_rejects_empty_code(client: TestClient) -> None:
    resp = client.post("/calendar/connect", json={"code": ""})
    assert resp.status_code == 422


def test_disconnect(client: TestClient) -> None:
    resp = client.delete("/calendar/connect")
    assert resp.status_code == 204


def test_freebusy(client: TestClient) -> None:
    resp = client.get("/calendar/freebusy", params={"from": "2026-05-25", "to": "2026-05-26"})
    assert resp.status_code == 200
    assert isinstance(resp.json()["busy"], list)


def test_freebusy_requires_range(client: TestClient) -> None:
    resp = client.get("/calendar/freebusy")
    assert resp.status_code == 422


def test_sync_preview(client: TestClient) -> None:
    resp = client.post("/calendar/sync-preview")
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert "conflictCount" in body


def test_approve_insert_requires_idempotency_key(client: TestClient) -> None:
    resp = client.post("/calendar/events/approve-insert")
    assert resp.status_code == 400
    assert resp.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_approve_insert_with_key(client: TestClient) -> None:
    resp = client.post(
        "/calendar/events/approve-insert", headers={"Idempotency-Key": "calendar-demo-1"}
    )
    assert resp.status_code == 200
    assert resp.json()["insertedCount"] == 2
