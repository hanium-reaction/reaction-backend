"""Privacy — #23-B 슬라이스 (S28, api-contract §16).

Consent(append-only) + 즉시 익명화(2단계 확인 토큰). 톤 prefix 배선은 별도 후속(ADR-0003).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from reaction_backend.auth.confirm import issue_confirmation_token, verify_confirmation_token
from reaction_backend.db.models.calendar_connection import CalendarConnection
from reaction_backend.db.models.user import User
from reaction_backend.integrations.google_calendar import oauth, token_store
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


# ─────────────── 2단계 확인 문구 — 사용자에게 그대로 보인다 (auth-10 / journey-8) ───────────────


@pytest.mark.parametrize("path", ["/settings/anonymize", "/settings/delete-account"])
def test_confirmation_message_is_user_facing(client: TestClient, path: str) -> None:
    """FE 가 step1 `message` 를 화면에 그대로 띄운다 — API 설명('확인 토큰으로 …')이 보였다."""
    body = client.post(path, json={}).json()

    assert body["status"] == "confirmation_required"
    assert "토큰" not in body["message"]
    assert "요청" not in body["message"]
    assert "되돌릴 수 없어요" in body["message"]
    assert "캘린더 연결도 해제" in body["message"]  # 무엇이 사라지는지 말한다


@pytest.mark.parametrize("path", ["/settings/anonymize", "/settings/delete-account"])
def test_expired_confirmation_message_has_no_jargon(client: TestClient, path: str) -> None:
    resp = client.post(path, json={"confirmationToken": "bad.token"})

    assert resp.status_code == 422
    assert resp.json()["code"] == "PRIVACY_INVALID_CONFIRMATION"
    assert "토큰" not in resp.json()["message"]


# ─────────────── 삭제만 나머지 텍스트까지 지운다 (auth-9) ───────────────


def test_delete_account_purges_remaining_text(
    client: TestClient, fake_privacy_repo: FakePrivacyRepo
) -> None:
    token = client.post("/settings/delete-account", json={}).json()["confirmationToken"]
    resp = client.post("/settings/delete-account", json={"confirmationToken": token})

    assert resp.status_code == 200
    assert fake_privacy_repo.anonymized_user == DEMO_USER_UUID
    assert fake_privacy_repo.purged_user == DEMO_USER_UUID


def test_anonymize_keeps_plan_structure(
    client: TestClient, fake_privacy_repo: FakePrivacyRepo
) -> None:
    """익명화는 계정을 계속 쓰는 사람용 — 목표·할 일 제목 등 계획 구조까지 지우지 않는다."""
    token = client.post("/settings/anonymize", json={}).json()["confirmationToken"]
    client.post("/settings/anonymize", json={"confirmationToken": token})

    assert fake_privacy_repo.anonymized_user == DEMO_USER_UUID
    assert fake_privacy_repo.purged_user is None


# ─────────────── 캘린더 권한 회수 — 세 진입점 공통 (auth-8 / calendar-3 / auth-15) ───────────────


class _CalendarSpy:
    """살아 있는 캘린더 연결 1건 + 원격 회수 호출 기록."""

    def __init__(self, sessions: list[Any] | None = None) -> None:
        self.connection = CalendarConnection()
        self.connection.revoked_at = None
        self.revoked_tokens: list[str] = []
        self.commits_at_revoke: list[int] = []
        self._sessions = sessions

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _get_active(session: Any, *, user_id: Any) -> CalendarConnection | None:
            return self.connection if self.connection.revoked_at is None else None

        async def _revoke(token: str) -> None:
            self.revoked_tokens.append(token)
            if self._sessions:
                self.commits_at_revoke.append(self._sessions[-1].commit_count)

        monkeypatch.setattr(token_store, "get_active", _get_active)
        # 마스킹 전에 읽은 **원래** 토큰이어야 한다 — 덮은 뒤엔 sentinel 이다.
        monkeypatch.setattr(token_store, "refresh_token_of", lambda c: "rt-original")
        monkeypatch.setattr(oauth, "revoke", _revoke)


@pytest.mark.parametrize("path", ["/settings/anonymize", "/settings/delete-account"])
def test_routes_revoke_calendar_grant_after_commit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_sessions: list[Any],
    path: str,
) -> None:
    """우리 쪽 연결을 먼저 끊어 커밋하고, 그 뒤 Google 쪽 권한을 원래 토큰으로 회수한다.

    예전엔 토큰만 sentinel 로 덮어 Google 에 권한이 영영 남았다(같은 계정 재가입 시 첫
    캘린더 연결 실패). 익명화 계정은 연결이 '연결됨' 으로 남아 매 조회가 실패했다.
    """
    spy = _CalendarSpy(fake_sessions)
    spy.install(monkeypatch)

    token = client.post(path, json={}).json()["confirmationToken"]
    resp = client.post(path, json={"confirmationToken": token})

    assert resp.status_code == 200
    assert spy.connection.revoked_at is not None
    assert spy.revoked_tokens == ["rt-original"]
    assert spy.commits_at_revoke and spy.commits_at_revoke[0] >= 1  # commit 뒤에 원격 회수


async def test_cron_applies_the_same_anonymization(monkeypatch: pytest.MonkeyPatch) -> None:
    """90일 cron 도 수동 익명화와 같은 함수 — 플래그·이름·캘린더 회수가 똑같이 적용된다."""
    from reaction_backend.scheduler.anonymize_inactive import run_anonymize_inactive_users
    from reaction_backend.schemas.common import now_kst
    from tests.conftest import FakeUserRepo, _FakeSession

    spy = _CalendarSpy()
    spy.install(monkeypatch)
    repo = FakeUserRepo()
    user = User()
    user.id = uuid4()
    user.email = f"{user.id}@test.local"
    user.name = "떠난 사용자"
    user.onboarding_state = "ACTIVE"
    user.is_anonymized = False
    user.anonymized_at = None
    user.last_active_at = now_kst() - timedelta(days=120)
    repo.register(user)
    privacy = FakePrivacyRepo()

    result = await run_anonymize_inactive_users(
        _FakeSession(), user_repo=repo, privacy_repo=privacy, now=now_kst()
    )

    assert result.anonymized == 1
    assert user.is_anonymized is True
    assert user.name == "[anonymized]"
    assert user.archived_at is None  # 삭제가 아니다
    assert privacy.purged_user is None
    assert spy.connection.revoked_at is not None
    assert spy.revoked_tokens == ["rt-original"]


# ─────────────── 비 ASCII 확인 토큰 (auth-14) ───────────────


@pytest.mark.parametrize("token", ["가.x", "abc.가", "é.é"])
def test_non_ascii_confirmation_token_is_just_invalid(token: str) -> None:
    """서버 토큰은 base64url(ASCII) — 다른 문자가 섞이면 예외가 아니라 '틀림' 이다."""
    assert verify_confirmation_token(token, uuid4(), _PURPOSE) is False


@pytest.mark.parametrize("path", ["/settings/anonymize", "/settings/delete-account"])
def test_non_ascii_confirmation_token_returns_422_not_500(client: TestClient, path: str) -> None:
    resp = client.post(path, json={"confirmationToken": "가.x"})

    assert resp.status_code == 422
    assert resp.json()["code"] == "PRIVACY_INVALID_CONFIRMATION"
