"""Notifications — settings + Web Push 구독 실 구현 (Issue #17·#16, api-contract §15)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from reaction_backend.db.models.user import User
from tests.conftest import DEMO_USER_UUID, FakeNotificationRepo

_SUBSCRIPTION = {"endpoint": "https://push.example.com/x", "keys": {"p256dh": "k", "auth": "a"}}


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


def test_subscribe_persists_subscription(
    client: TestClient, fake_notification_repo: FakeNotificationRepo
) -> None:
    """구독 객체가 실제로 저장된다 — mock 시절엔 201 만 주고 아무것도 안 남았다."""
    resp = client.post("/notifications/subscribe", json=_SUBSCRIPTION)
    assert resp.status_code == 201
    assert resp.json()["pushSubscribed"] is True

    stored = fake_notification_repo._items[DEMO_USER_UUID].push_subscription
    assert stored == _SUBSCRIPTION  # pywebpush 가 그대로 받는 {endpoint, keys}


def test_subscribe_response_reflects_real_settings(client: TestClient) -> None:
    """응답이 실 설정 행 기준 — mock 은 09:00 으로 바꿔도 DEMO 고정값(08:00)을 돌려줬다."""
    client.patch("/notifications/settings", json={"morningBriefTime": "09:00"})
    resp = client.post("/notifications/subscribe", json=_SUBSCRIPTION)
    assert resp.json()["morningBriefTime"] == "09:00"


def test_subscribe_rejects_missing_webpush_keys(client: TestClient) -> None:
    """p256dh/auth 없는 구독 객체는 저장 전에 422 — 발송 시점 crash 예방."""
    resp = client.post(
        "/notifications/subscribe",
        json={"endpoint": "https://push.example.com/x", "keys": {"p256dh": "k"}},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_unsubscribe_clears_subscription(
    client: TestClient, fake_notification_repo: FakeNotificationRepo
) -> None:
    client.post("/notifications/subscribe", json=_SUBSCRIPTION)
    resp = client.delete("/notifications/subscribe")
    assert resp.status_code == 204
    assert fake_notification_repo._items[DEMO_USER_UUID].push_subscription is None

    check = client.get("/notifications/settings")
    assert check.json()["pushSubscribed"] is False


def test_unsubscribe_is_idempotent_without_subscription(client: TestClient) -> None:
    """구독한 적 없어도 204 — FE 가 상태 확인 없이 안전하게 호출할 수 있게."""
    resp = client.delete("/notifications/subscribe")
    assert resp.status_code == 204
