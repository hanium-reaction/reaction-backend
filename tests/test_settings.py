"""Settings — S23 실구현 + S28 Privacy 501 스텁 (Issue #23-A, api-contract §16)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from reaction_backend.db.models.user import User


def test_get_settings_returns_tone_language_timezone(client: TestClient) -> None:
    """demo user(tone=gentle, tz=Asia/Seoul) → language 는 ko 고정, 알림 행 없으면 null."""
    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["toneMode"] == "gentle"
    assert body["language"] == "ko"
    assert body["timezone"] == "Asia/Seoul"
    assert body["notifications"] is None


def test_get_settings_includes_notification_summary_when_set(client: TestClient) -> None:
    """알림 설정 후 GET /settings 요약에 반영 (읽기 전용)."""
    client.patch("/notifications/settings", json={"morningBriefTime": "09:00"})
    resp = client.get("/settings")
    summary = resp.json()["notifications"]
    assert summary is not None
    assert summary["morningBriefTime"] == "09:00"
    assert summary["eveningReflectionTime"] == "21:00"
    assert summary["preCardEnabled"] is False


def test_get_settings_does_not_create_notification_row(client: TestClient) -> None:
    """GET 은 부작용 없음 — 호출 후에도 알림 행이 생기지 않는다 (여전히 null)."""
    client.get("/settings")
    resp = client.get("/settings")
    assert resp.json()["notifications"] is None


def test_patch_tone_mode_updates(client: TestClient) -> None:
    resp = client.patch("/settings/tone-mode", json={"toneMode": "strict"})
    assert resp.status_code == 200
    assert resp.json()["toneMode"] == "strict"


def test_patch_tone_mode_persists_on_user(client: TestClient, demo_user_orm: User) -> None:
    client.patch("/settings/tone-mode", json={"toneMode": "encouraging"})
    assert demo_user_orm.tone_mode == "encouraging"


def test_patch_tone_mode_persists_via_get(client: TestClient) -> None:
    client.patch("/settings/tone-mode", json={"toneMode": "strict"})
    resp = client.get("/settings")
    assert resp.json()["toneMode"] == "strict"


def test_patch_tone_mode_rejects_invalid(client: TestClient) -> None:
    resp = client.patch("/settings/tone-mode", json={"toneMode": "aggressive"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_get_settings_requires_auth(unauthed_client: TestClient) -> None:
    resp = unauthed_client.get("/settings")
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_TOKEN"


# S28 Privacy(anonymize·consent)는 #23-B 에서 실구현 — 검증은 tests/test_privacy.py.
