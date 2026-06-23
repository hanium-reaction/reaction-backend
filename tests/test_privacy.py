"""Privacy — #23-B 슬라이스 (S28, api-contract §16).

Consent(append-only) + 즉시 익명화(2단계 확인 토큰). 톤 prefix 배선은 별도 후속(ADR-0003).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from reaction_backend.auth.confirm import issue_confirmation_token, verify_confirmation_token
from reaction_backend.db.models.user import User
from tests.conftest import DEMO_USER_UUID, FakePrivacyRepo

_PURPOSE = "anonymize"


# ───────────────────────── 확인 토큰 (순수) ─────────────────────────


def test_confirm_token_roundtrip() -> None:
    token, _ = issue_confirmation_token(DEMO_USER_UUID, _PURPOSE)
    assert verify_confirmation_token(token, DEMO_USER_UUID, _PURPOSE)


def test_confirm_token_wrong_user() -> None:
    token, _ = issue_confirmation_token(DEMO_USER_UUID, _PURPOSE)
    assert not verify_confirmation_token(token, uuid4(), _PURPOSE)


def test_confirm_token_wrong_purpose() -> None:
    token, _ = issue_confirmation_token(DEMO_USER_UUID, _PURPOSE)
    assert not verify_confirmation_token(token, DEMO_USER_UUID, "delete")


def test_confirm_token_tampered() -> None:
    token, _ = issue_confirmation_token(DEMO_USER_UUID, _PURPOSE)
    assert not verify_confirmation_token(token + "x", DEMO_USER_UUID, _PURPOSE)


def test_confirm_token_expired() -> None:
    past = datetime(2020, 1, 1, tzinfo=UTC)
    token, _ = issue_confirmation_token(DEMO_USER_UUID, _PURPOSE, now=past)
    assert not verify_confirmation_token(
        token, DEMO_USER_UUID, _PURPOSE, now=datetime(2026, 1, 1, tzinfo=UTC)
    )


# ───────────────────────── GET/POST /privacy/consent ─────────────────────────


def test_consent_empty(client: TestClient) -> None:
    resp = client.get("/privacy/consent")
    assert resp.status_code == 200
    assert resp.json()["consents"] == []


def test_consent_add_and_list(client: TestClient) -> None:
    resp = client.post("/privacy/consent", json={"consentType": "marketing", "granted": True})
    assert resp.status_code == 200
    marketing = [c for c in resp.json()["consents"] if c["consentType"] == "marketing"]
    assert len(marketing) == 1
    assert marketing[0]["isGranted"] is True


def test_consent_latest_wins(client: TestClient) -> None:
    client.post("/privacy/consent", json={"consentType": "research", "granted": True})
    client.post("/privacy/consent", json={"consentType": "research", "granted": False})
    resp = client.get("/privacy/consent")
    research = [c for c in resp.json()["consents"] if c["consentType"] == "research"]
    assert len(research) == 1  # append-only지만 최신 1행만 노출
    assert research[0]["isGranted"] is False


def test_consent_bad_type(client: TestClient) -> None:
    resp = client.post("/privacy/consent", json={"consentType": "spam", "granted": True})
    assert resp.status_code == 422


def test_consent_requires_auth(unauthed_client: TestClient) -> None:
    assert unauthed_client.get("/privacy/consent").status_code == 401


# ───────────────────────── POST /settings/anonymize (2단계) ─────────────────────────


def test_anonymize_step1_issues_token(client: TestClient, demo_user_orm: User) -> None:
    resp = client.post("/settings/anonymize", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "confirmation_required"
    assert body["confirmationToken"]
    assert demo_user_orm.is_anonymized in (None, False)  # 아직 미적용


def test_anonymize_two_step_applies(
    client: TestClient, demo_user_orm: User, fake_privacy_repo: FakePrivacyRepo
) -> None:
    token = client.post("/settings/anonymize", json={}).json()["confirmationToken"]
    resp = client.post("/settings/anonymize", json={"confirmationToken": token})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "anonymized"
    assert body["maskedCount"] == 3
    assert demo_user_orm.is_anonymized is True
    assert demo_user_orm.name == "[anonymized]"
    assert fake_privacy_repo.anonymized_user == demo_user_orm.id


def test_anonymize_invalid_token(client: TestClient) -> None:
    resp = client.post("/settings/anonymize", json={"confirmationToken": "bad.token"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "PRIVACY_INVALID_CONFIRMATION"


def test_anonymize_already(client: TestClient, demo_user_orm: User) -> None:
    demo_user_orm.is_anonymized = True
    resp = client.post("/settings/anonymize", json={})
    assert resp.status_code == 409
    assert resp.json()["code"] == "PRIVACY_ALREADY_ANONYMIZED"


def test_anonymize_requires_auth(unauthed_client: TestClient) -> None:
    assert unauthed_client.post("/settings/anonymize", json={}).status_code == 401
