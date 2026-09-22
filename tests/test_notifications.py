"""Notifications — settings + Web Push 구독 실 구현 (Issue #17·#16, api-contract §15)."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from reaction_backend.config import get_settings
from reaction_backend.db.models.notification_send import NotificationSend
from reaction_backend.db.models.notification_setting import NotificationSetting
from reaction_backend.db.models.user import User
from reaction_backend.schemas.common import KST
from tests.conftest import DEMO_USER_UUID, FakeNotificationRepo, FakeNotificationSendRepo

_SUBSCRIPTION = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/x",
    "keys": {"p256dh": "k", "auth": "a"},
}


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
        json={"endpoint": "https://fcm.googleapis.com/fcm/send/x", "keys": {"p256dh": "k"}},
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


def test_vapid_public_key_returned_when_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """서버가 자기 public key 를 알려준다 — FE 가 rotate 에도 따라오게 (하드코딩 제거)."""
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BExamplePublicKeyForTest123")
    get_settings.cache_clear()

    resp = client.get("/notifications/vapid-public-key")
    assert resp.status_code == 200
    assert resp.json()["publicKey"] == "BExamplePublicKeyForTest123"


def test_vapid_public_key_null_when_unconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """미설정이면 null — FE 는 도달 못 하는 구독을 만들지 않는다 (403 무한 재시도 방지).

    빈 문자열이 아니라 null 로 내려야 FE 가 '미설정' 을 명확히 분기할 수 있다.
    """
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "")
    get_settings.cache_clear()

    resp = client.get("/notifications/vapid-public-key")
    assert resp.status_code == 200
    assert resp.json()["publicKey"] is None


# ── POST /notifications/{id}/opened (근거 대장 §6.1 — 아직 FE 콜백 없는 인프라) ──


def _seed_notification(
    repo: FakeNotificationSendRepo,
    *,
    user_id: object = DEMO_USER_UUID,
    sent_at: datetime = datetime(2026, 7, 21, 21, 0, tzinfo=KST),
) -> NotificationSend:
    row = NotificationSend()
    row.id = uuid4()
    row.user_id = user_id
    row.notification_class = "evening_reflection"
    row.sent_at = sent_at
    row.target_action_item_id = None
    row.opened_at = None
    repo._sends.append(row)
    return row


def test_mark_opened_returns_204_and_stamps_opened_at(
    client: TestClient, fake_notification_send_repo: FakeNotificationSendRepo
) -> None:
    row = _seed_notification(fake_notification_send_repo)

    resp = client.post(f"/notifications/notif_{row.id}/opened")

    assert resp.status_code == 204
    assert row.opened_at is not None


def test_mark_opened_is_idempotent_keeps_first_open_time(
    client: TestClient, fake_notification_send_repo: FakeNotificationSendRepo
) -> None:
    row = _seed_notification(fake_notification_send_repo)

    first = client.post(f"/notifications/notif_{row.id}/opened")
    first_opened_at = row.opened_at
    second = client.post(f"/notifications/notif_{row.id}/opened")

    assert first.status_code == 204
    assert second.status_code == 204
    assert row.opened_at == first_opened_at, "재클릭이 최초 오픈 시각을 덮어썼다"


def test_mark_opened_404_for_unknown_id(client: TestClient) -> None:
    resp = client.post(f"/notifications/notif_{uuid4()}/opened")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOTIF_NOT_FOUND"


def test_mark_opened_404_for_malformed_id(client: TestClient) -> None:
    """접두어(`notif_`)가 없거나 UUID 가 아니면 — 존재하는 다른 알림 id 를 흘리지 않는다."""
    resp = client.post("/notifications/not-a-real-id/opened")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOTIF_NOT_FOUND"


def test_mark_opened_404_for_another_users_notification(
    client: TestClient, fake_notification_send_repo: FakeNotificationSendRepo
) -> None:
    """다른 사용자의 발송 이력은 내가 못 연다 — id 를 안다고 다 여는 게 아니다."""
    row = _seed_notification(fake_notification_send_repo, user_id=uuid4())

    resp = client.post(f"/notifications/notif_{row.id}/opened")

    assert resp.status_code == 404
    assert row.opened_at is None


# ───── 구독 endpoint 허용 목록 (sched-5 / abuse-10 — blind SSRF 차단) ─────


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://169.254.169.254/latest/meta-data/",  # 인스턴스 메타데이터
        "https://169.254.169.254/x",  # https 라도 IP 리터럴
        "http://127.0.0.1:2019/stop",  # 내부 서비스
        "https://127.0.0.1/push",
        "http://fcm.googleapis.com/fcm/send/abc",  # 알려진 호스트라도 http
        "https://evil.example.com/push",  # 허용 목록 밖
        "https://evilpush.apple.com/x",  # 접미사 흉내
        "https://fcm.googleapis.com@evil.example.com/x",  # userinfo 혼동
        "https://fcm.googleapis.com:8443/fcm/send/abc",  # 다른 포트
    ],
)
def test_subscribe_rejects_non_push_service_endpoint(
    client: TestClient, fake_notification_repo: FakeNotificationRepo, endpoint: str
) -> None:
    resp = client.post(
        "/notifications/subscribe",
        json={"endpoint": endpoint, "keys": {"p256dh": "k", "auth": "a"}},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "COMMON_VALIDATION_ERROR"
    assert body["field"] == "endpoint"
    assert "다시 켜 주세요" in body["message"]
    setting = fake_notification_repo._items.get(DEMO_USER_UUID)
    assert setting is None or setting.push_subscription is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/fcm/send/abc:APA91b",
        "https://updates.push.services.mozilla.com/wpush/v2/gAAAA",
        "https://web.push.apple.com/QGx",
        "https://db5p.notify.windows.com/w/?token=abc",
    ],
)
def test_subscribe_accepts_real_push_services(client: TestClient, endpoint: str) -> None:
    resp = client.post(
        "/notifications/subscribe",
        json={"endpoint": endpoint, "keys": {"p256dh": "k", "auth": "a"}},
    )
    assert resp.status_code == 201
    assert resp.json()["pushSubscribed"] is True


def test_subscribe_takes_the_endpoint_away_from_other_users(
    client: TestClient, fake_notification_repo: FakeNotificationRepo
) -> None:
    """같은 브라우저(endpoint)는 한 사람 몫 — 마지막으로 켠 사람에게 간다 (sched-4)."""
    previous_owner = uuid4()
    their = fake_notification_repo._items.setdefault(previous_owner, NotificationSetting())
    their.user_id = previous_owner
    their.push_subscription = dict(_SUBSCRIPTION)
    elsewhere = uuid4()
    other = fake_notification_repo._items.setdefault(elsewhere, NotificationSetting())
    other.user_id = elsewhere
    other.push_subscription = {
        "endpoint": "https://fcm.googleapis.com/fcm/send/other-phone",
        "keys": {"p256dh": "k", "auth": "a"},
    }

    resp = client.post("/notifications/subscribe", json=_SUBSCRIPTION)

    assert resp.status_code == 201
    assert their.push_subscription is None
    assert other.push_subscription is not None
