"""Notifications — settings 실 구현 + subscribe mock (Issue #17, api-contract §15)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from reaction_backend.db.models.user import User


def test_get_settings_returns_defaults_for_new_user(client: TestClient) -> None:
    """첫 GET 은 default 값(get_or_create)으로 1행 생성."""
    resp = client.get("/notifications/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["morningBriefTime"] == "08:00"
    assert body["eveningReflectionTime"] == "21:00"
    assert body["preCardEnabled"] is False
    assert body["pushSubscribed"] is False


def test_update_settings_morning(client: TestClient) -> None:
    resp = client.patch("/notifications/settings", json={"morningBriefTime": "09:00"})
    assert resp.status_code == 200
    assert resp.json()["morningBriefTime"] == "09:00"


def test_update_settings_evening(client: TestClient) -> None:
    resp = client.patch("/notifications/settings", json={"eveningReflectionTime": "22:00"})
    assert resp.status_code == 200
    assert resp.json()["eveningReflectionTime"] == "22:00"


def test_update_settings_pre_card(client: TestClient) -> None:
    resp = client.patch("/notifications/settings", json={"preCardEnabled": True})
    assert resp.status_code == 200
    assert resp.json()["preCardEnabled"] is True


def test_update_settings_rejects_morning_out_of_range(client: TestClient) -> None:
    resp = client.patch("/notifications/settings", json={"morningBriefTime": "05:00"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "NOTIF_TIME_RANGE"
    assert resp.json()["field"] == "morningBriefTime"


def test_update_settings_rejects_evening_out_of_range(client: TestClient) -> None:
    resp = client.patch("/notifications/settings", json={"eveningReflectionTime": "18:30"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "NOTIF_TIME_RANGE"


def test_update_settings_rejects_bad_format(client: TestClient) -> None:
    resp = client.patch("/notifications/settings", json={"morningBriefTime": "9am"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_update_settings_persists(client: TestClient) -> None:
    client.patch("/notifications/settings", json={"morningBriefTime": "09:00"})
    resp = client.get("/notifications/settings")
    assert resp.json()["morningBriefTime"] == "09:00"


def test_patch_advances_onboarding_to_active(client: TestClient, demo_user_orm: User) -> None:
    """ONBOARDING_NOTIFICATIONS → ACTIVE 멱등 전이."""
    demo_user_orm.onboarding_state = "ONBOARDING_NOTIFICATIONS"
    client.patch("/notifications/settings", json={"morningBriefTime": "09:00"})
    assert demo_user_orm.onboarding_state == "ACTIVE"


def test_subscribe_mock(client: TestClient) -> None:
    """Web Push subscribe — Issue #25 (PWA) 까지 mock 유지."""
    resp = client.post(
        "/notifications/subscribe",
        json={"endpoint": "https://push.example.com/x", "keys": {"p256dh": "k", "auth": "a"}},
    )
    assert resp.status_code == 201
    assert resp.json()["pushSubscribed"] is True


def test_unsubscribe_mock(client: TestClient) -> None:
    resp = client.delete("/notifications/subscribe")
    assert resp.status_code == 204
