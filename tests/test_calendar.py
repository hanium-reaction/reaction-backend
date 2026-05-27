"""Calendar — connect/disconnect P1 (Issue #17), freebusy/sync-preview/approve-insert mock 유지."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_connect_returns_501_p1(client: TestClient) -> None:
    """Google Calendar OAuth 는 P1 — 베타 이후 지원."""
    resp = client.post("/calendar/connect", json={"code": "oauth-code"})
    assert resp.status_code == 501
    assert resp.json()["code"] == "COMMON_NOT_IMPLEMENTED"


def test_connect_rejects_empty_code(client: TestClient) -> None:
    """Pydantic Field min_length=1 — 본문 검증이 라우터 진입 전에 422."""
    resp = client.post("/calendar/connect", json={"code": ""})
    assert resp.status_code == 422


def test_disconnect_returns_501_p1(client: TestClient) -> None:
    resp = client.delete("/calendar/connect")
    assert resp.status_code == 501
    assert resp.json()["code"] == "COMMON_NOT_IMPLEMENTED"


def test_freebusy_mock(client: TestClient) -> None:
    """freebusy 는 #18 First Plan 흐름에서 다시 — 현재 mock 응답."""
    resp = client.get("/calendar/freebusy", params={"from": "2026-05-25", "to": "2026-05-26"})
    assert resp.status_code == 200
    assert isinstance(resp.json()["busy"], list)


def test_freebusy_requires_range(client: TestClient) -> None:
    resp = client.get("/calendar/freebusy")
    assert resp.status_code == 422


def test_sync_preview_mock(client: TestClient) -> None:
    resp = client.post("/calendar/sync-preview")
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert "conflictCount" in body


def test_approve_insert_requires_idempotency_key(client: TestClient) -> None:
    resp = client.post("/calendar/events/approve-insert")
    assert resp.status_code == 400
    assert resp.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_approve_insert_with_key_mock(client: TestClient) -> None:
    resp = client.post(
        "/calendar/events/approve-insert", headers={"Idempotency-Key": "calendar-demo-1"}
    )
    assert resp.status_code == 200
    assert resp.json()["insertedCount"] == 2
