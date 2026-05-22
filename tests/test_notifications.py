"""Notifications 스텁 (api-contract §15 / #3-C)."""

from fastapi.testclient import TestClient


def test_get_settings(client: TestClient) -> None:
    resp = client.get("/notifications/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert "morningBriefTime" in body
    assert "pushSubscribed" in body


def test_update_settings(client: TestClient) -> None:
    resp = client.patch("/notifications/settings", json={"morningBriefTime": "09:00"})
    assert resp.status_code == 200
    assert resp.json()["morningBriefTime"] == "09:00"


def test_update_settings_rejects_out_of_range(client: TestClient) -> None:
    resp = client.patch("/notifications/settings", json={"morningBriefTime": "05:00"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "NOTIF_TIME_RANGE"


def test_update_settings_rejects_bad_format(client: TestClient) -> None:
    resp = client.patch("/notifications/settings", json={"morningBriefTime": "9am"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_subscribe(client: TestClient) -> None:
    resp = client.post(
        "/notifications/subscribe",
        json={"endpoint": "https://push.example.com/x", "keys": {"p256dh": "k", "auth": "a"}},
    )
    assert resp.status_code == 201
    assert resp.json()["pushSubscribed"] is True


def test_unsubscribe(client: TestClient) -> None:
    resp = client.delete("/notifications/subscribe")
    assert resp.status_code == 204
