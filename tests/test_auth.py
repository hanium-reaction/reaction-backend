"""Auth — Google OAuth + JWT 세션 실구현 (Issue #16) + 가입 게이트 (#324).

`auth_client` fixture: repo/session 만 override, 인증은 실제 JWT 흐름.
stub 모드에서 verifier 가 고정 demo 클레임 반환 → FakeUserRepo 가 user 생성.

신규 가입(email 이 처음 보이는 로그인)은 이제 유효한 초대코드가 있어야 한다 — 그래서
"stub" id_token 을 재사용하는 기존 테스트들도 대부분 `_login` 헬퍼로 코드를 미리 심고
호출한다. 기존 사용자 로그인(같은 email 두 번째 이후)은 게이트를 아예 안 거치므로
코드 없이도 통과한다(`test_google_login_existing_user_*` 가 그 계약을 고정한다).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from reaction_backend.config import get_settings
from tests.conftest import DEMO_USER_UUID, FakeInviteCodeRepo, FakeUserRepo, issue_helper_token


def _login(
    client: TestClient,
    invite_repo: FakeInviteCodeRepo,
    id_token: str = "stub",
    *,
    invite_code: str = "TESTCODE",
) -> Any:
    """게이트를 통과하는 로그인 — 신규 가입 대상 email 이면 코드를 미리 심어 둔다.

    기존 사용자 재로그인(같은 id_token 재호출 등)에도 코드를 같이 보내지만, 그 경로는
    게이트 자체를 안 타므로 무해하다(라우터가 email 존재를 먼저 확인).
    """
    invite_repo.seed(invite_code)
    resp = client.post("/auth/google", json={"idToken": id_token, "inviteCode": invite_code})
    assert resp.status_code == 200, resp.json()
    return resp.json()


def test_google_login_creates_new_user(
    auth_client: TestClient, fake_invite_code_repo: FakeInviteCodeRepo
) -> None:
    body = _login(auth_client, fake_invite_code_repo, "stub-id-token")
    assert body["accessToken"]
    assert body["refreshToken"]
    # stub 모드 verifier 의 고정 클레임
    assert body["user"]["email"] == "demo@reaction.local"
    # 신규 user 는 WELCOME 상태로 생성됨
    assert body["user"]["onboardingState"] == "WELCOME"
    assert body["user"]["userId"].startswith("user_")


def test_google_login_rejects_empty_id_token(auth_client: TestClient) -> None:
    resp = auth_client.post("/auth/google", json={"idToken": ""})
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_google_login_reuses_existing_user(
    auth_client: TestClient,
    fake_user_repo: FakeUserRepo,
    fake_invite_code_repo: FakeInviteCodeRepo,
) -> None:
    """같은 email 로 두 번 로그인 — 동일 user_id, onboarding_state 보존."""
    first = _login(auth_client, fake_invite_code_repo, "stub")
    second = auth_client.post("/auth/google", json={"idToken": "stub"}).json()
    assert first["user"]["userId"] == second["user"]["userId"]
    assert len(fake_user_repo._by_email) == 1


def test_stub_device_token_creates_isolated_users(
    auth_client: TestClient,
    fake_user_repo: FakeUserRepo,
    fake_invite_code_repo: FakeInviteCodeRepo,
) -> None:
    """`demo:<id>` — 브라우저별 격리 데모 계정 (테스터 충돌 방지). 서로 다른 email 이라
    각자 자기 코드를 소비한다."""
    a = _login(auth_client, fake_invite_code_repo, "demo:tester-one", invite_code="CODE-ONE")
    b = _login(auth_client, fake_invite_code_repo, "demo:tester-two", invite_code="CODE-TWO")
    again = auth_client.post("/auth/google", json={"idToken": "demo:tester-one"}).json()

    assert a["user"]["email"] == "demo+tester-one@reaction.local"
    assert b["user"]["email"] == "demo+tester-two@reaction.local"
    assert a["user"]["userId"] != b["user"]["userId"]  # 서로 다른 유저
    assert again["user"]["userId"] == a["user"]["userId"]  # 같은 id 는 같은 유저
    assert len(fake_user_repo._by_email) == 2


def test_stub_plain_token_keeps_fixed_demo_account(
    auth_client: TestClient, fake_invite_code_repo: FakeInviteCodeRepo
) -> None:
    """`demo:` 접두사가 아니면 종전대로 고정 demo 계정 — 시드 시나리오 계정 유지."""
    res = _login(auth_client, fake_invite_code_repo, "anything-else")
    assert res["user"]["email"] == "demo@reaction.local"

    # 접두사만 있고 id 가 비면(정규화 후 빈 slug) 고정 계정으로 fallback — 같은 email 이라
    # 이미 위에서 가입한 기존 사용자 재로그인이 된다(코드 불필요).
    edge = auth_client.post("/auth/google", json={"idToken": "demo:!!!"}).json()
    assert edge["user"]["email"] == "demo@reaction.local"


def test_refresh_returns_new_access(
    auth_client: TestClient, fake_invite_code_repo: FakeInviteCodeRepo
) -> None:
    login = _login(auth_client, fake_invite_code_repo)
    resp = auth_client.post(
        "/auth/refresh",
        json={"refreshToken": login["refreshToken"]},
    )
    assert resp.status_code == 200
    assert resp.json()["accessToken"]


def test_refresh_with_invalid_token(auth_client: TestClient) -> None:
    resp = auth_client.post("/auth/refresh", json={"refreshToken": "not-a-jwt"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_TOKEN"


def test_refresh_with_expired_token(auth_client: TestClient) -> None:
    expired = issue_helper_token(user_id=DEMO_USER_UUID, token_type="refresh", expired=True)
    resp = auth_client.post("/auth/refresh", json={"refreshToken": expired})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_EXPIRED"


def test_refresh_with_access_token_rejected(
    auth_client: TestClient, fake_invite_code_repo: FakeInviteCodeRepo
) -> None:
    """access 토큰을 refresh 자리에 보내면 type mismatch → INVALID_TOKEN."""
    login = _login(auth_client, fake_invite_code_repo)
    resp = auth_client.post(
        "/auth/refresh",
        json={"refreshToken": login["accessToken"]},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_TOKEN"


def test_logout_revokes_refresh(
    auth_client: TestClient, fake_invite_code_repo: FakeInviteCodeRepo
) -> None:
    login = _login(auth_client, fake_invite_code_repo)
    refresh = login["refreshToken"]

    logout_resp = auth_client.post("/auth/logout", json={"refreshToken": refresh})
    assert logout_resp.status_code == 204

    second = auth_client.post("/auth/refresh", json={"refreshToken": refresh})
    assert second.status_code == 401
    assert second.json()["code"] == "AUTH_INVALID_TOKEN"


def test_logout_idempotent_with_invalid_token(auth_client: TestClient) -> None:
    """잘못된 토큰이어도 logout 은 204 (멱등)."""
    resp = auth_client.post("/auth/logout", json={"refreshToken": "junk"})
    assert resp.status_code == 204


def test_me_returns_profile_with_valid_token(
    auth_client: TestClient, fake_invite_code_repo: FakeInviteCodeRepo
) -> None:
    login = _login(auth_client, fake_invite_code_repo)
    access = login["accessToken"]
    resp = auth_client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "demo@reaction.local"
    assert body["onboardingState"] == "WELCOME"


def test_me_without_token_returns_401(auth_client: TestClient) -> None:
    resp = auth_client.get("/auth/me")
    assert resp.status_code == 401


# ───────────────────────── POST /settings/delete-account (#321) ─────────────────────────


def _delete_account(
    client: TestClient, access: str, *, confirmation_token: str | None = None
) -> Any:
    body: dict[str, str] = {}
    if confirmation_token is not None:
        body["confirmationToken"] = confirmation_token
    return client.post(
        "/settings/delete-account",
        json=body,
        headers={"Authorization": f"Bearer {access}"},
    )


def test_deleted_account_access_token_stops_working(
    auth_client: TestClient, fake_invite_code_repo: FakeInviteCodeRepo
) -> None:
    """삭제 전 발급된 access token 은 삭제 다음 요청부터 401 이다.

    새 토큰 블랙리스트가 아니라 `get_current_user` → `UserRepo.get_by_id` 의 기존
    `archived_at IS NULL` 필터 하나로 막힌다는 것이 이 테스트가 고정하는 계약이다.
    """
    login = _login(auth_client, fake_invite_code_repo)
    access = login["accessToken"]

    pre = auth_client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert pre.status_code == 200

    token = _delete_account(auth_client, access).json()["confirmationToken"]
    delete_resp = _delete_account(auth_client, access, confirmation_token=token)
    assert delete_resp.status_code == 200
    assert delete_resp.json()["status"] == "deleted"

    post = auth_client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert post.status_code == 401
    assert post.json()["code"] == "AUTH_INVALID_TOKEN"


def test_deleted_account_refresh_token_stops_working(
    auth_client: TestClient, fake_invite_code_repo: FakeInviteCodeRepo
) -> None:
    """삭제 전 발급된 refresh token 도 삭제 뒤엔 401 — jti 를 몰라도 막힌다."""
    login = _login(auth_client, fake_invite_code_repo)
    access = login["accessToken"]
    refresh = login["refreshToken"]

    token = _delete_account(auth_client, access).json()["confirmationToken"]
    _delete_account(auth_client, access, confirmation_token=token)

    resp = auth_client.post("/auth/refresh", json={"refreshToken": refresh})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_TOKEN"


def test_delete_account_masks_email_and_name(
    auth_client: TestClient, fake_invite_code_repo: FakeInviteCodeRepo, fake_user_repo: FakeUserRepo
) -> None:
    login = _login(auth_client, fake_invite_code_repo)
    access = login["accessToken"]
    user_id = login["user"]["userId"].removeprefix("user_")

    token = _delete_account(auth_client, access).json()["confirmationToken"]
    _delete_account(auth_client, access, confirmation_token=token)

    # 삭제된 계정은 public get_by_id 에서 이제 안 보인다(#321) — 내부 상태는 직접 확인.
    stored = fake_user_repo._by_id[UUID(user_id)]
    assert stored.is_anonymized is True
    assert stored.name == "[anonymized]"
    assert stored.email == f"deleted-{user_id}@reaction.invalid"
    assert stored.archived_at is not None


def test_delete_account_step1_issues_token_without_applying(
    auth_client: TestClient, fake_invite_code_repo: FakeInviteCodeRepo
) -> None:
    login = _login(auth_client, fake_invite_code_repo)
    access = login["accessToken"]

    resp = _delete_account(auth_client, access)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "confirmation_required"
    assert body["confirmationToken"]

    # 아직 적용 안 됐으니 토큰은 여전히 산다.
    still_alive = auth_client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert still_alive.status_code == 200


def test_delete_account_invalid_token(
    auth_client: TestClient, fake_invite_code_repo: FakeInviteCodeRepo
) -> None:
    login = _login(auth_client, fake_invite_code_repo)
    resp = _delete_account(auth_client, login["accessToken"], confirmation_token="bad.token")
    assert resp.status_code == 422
    assert resp.json()["code"] == "PRIVACY_INVALID_CONFIRMATION"


def test_delete_account_requires_auth(auth_client: TestClient) -> None:
    resp = auth_client.post("/settings/delete-account", json={})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_TOKEN"


def test_me_with_malformed_header_returns_401(auth_client: TestClient) -> None:
    resp = auth_client.get("/auth/me", headers={"Authorization": "Token abc"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_TOKEN"


def test_me_with_expired_token_returns_401_expired(auth_client: TestClient) -> None:
    expired = issue_helper_token(user_id=DEMO_USER_UUID, token_type="access", expired=True)
    resp = auth_client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_EXPIRED"


def test_me_with_refresh_token_rejected(
    auth_client: TestClient, fake_invite_code_repo: FakeInviteCodeRepo
) -> None:
    """refresh 토큰을 access 자리에 보내면 type mismatch → INVALID_TOKEN."""
    login = _login(auth_client, fake_invite_code_repo)
    refresh = login["refreshToken"]
    resp = auth_client.get("/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_TOKEN"


# ───────────────────────── 가입 게이트 (#324, FE #237 §8) ─────────────────────────


def test_new_signup_requires_invite_code(auth_client: TestClient) -> None:
    resp = auth_client.post("/auth/google", json={"idToken": "stub"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "AUTH_INVALID_INVITE_CODE"


def test_new_signup_rejects_unknown_invite_code(auth_client: TestClient) -> None:
    resp = auth_client.post("/auth/google", json={"idToken": "stub", "inviteCode": "NO-SUCH-CODE"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "AUTH_INVALID_INVITE_CODE"


def test_new_signup_rejects_already_used_invite_code(
    auth_client: TestClient, fake_invite_code_repo: FakeInviteCodeRepo
) -> None:
    fake_invite_code_repo.seed("USED-CODE", used=True)
    resp = auth_client.post("/auth/google", json={"idToken": "stub", "inviteCode": "USED-CODE"})
    assert resp.status_code == 409
    assert resp.json()["code"] == "AUTH_INVITE_CODE_ALREADY_USED"


def test_new_signup_invite_code_is_case_and_whitespace_insensitive(
    auth_client: TestClient, fake_invite_code_repo: FakeInviteCodeRepo
) -> None:
    fake_invite_code_repo.seed("REACTION-REVIEWER")
    resp = auth_client.post(
        "/auth/google", json={"idToken": "stub", "inviteCode": " reaction-reviewer "}
    )
    assert resp.status_code == 200, resp.json()


def test_new_signup_marks_invite_code_used_by_new_user(
    auth_client: TestClient, fake_invite_code_repo: FakeInviteCodeRepo
) -> None:
    body = _login(auth_client, fake_invite_code_repo, invite_code="ONETIME")
    row = fake_invite_code_repo._by_code["ONETIME"]
    assert row.used_at is not None
    assert f"user_{row.used_by_user_id}" == body["user"]["userId"]

    # 같은 코드로 다른 신규 email 재시도 — 이미 소진됐으니 409.
    again = auth_client.post(
        "/auth/google", json={"idToken": "demo:someone-else", "inviteCode": "ONETIME"}
    )
    assert again.status_code == 409
    assert again.json()["code"] == "AUTH_INVITE_CODE_ALREADY_USED"


def test_existing_user_login_ignores_missing_invite_code(
    auth_client: TestClient, fake_invite_code_repo: FakeInviteCodeRepo
) -> None:
    """완료 조건 — 기존 사용자 로그인은 게이트 영향을 전혀 받지 않는다."""
    _login(auth_client, fake_invite_code_repo)  # 최초 가입(코드 필요)
    second = auth_client.post("/auth/google", json={"idToken": "stub"})  # 코드 없이 재로그인
    assert second.status_code == 200


def test_existing_user_login_unaffected_by_signups_disabled(
    auth_client: TestClient,
    fake_invite_code_repo: FakeInviteCodeRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _login(auth_client, fake_invite_code_repo)  # 가입은 게이트가 열려 있을 때 먼저 끝낸다
    monkeypatch.setenv("SIGNUPS_ENABLED", "false")
    get_settings.cache_clear()
    resp = auth_client.post("/auth/google", json={"idToken": "stub"})
    assert resp.status_code == 200
    get_settings.cache_clear()


def test_new_signup_blocked_when_signups_disabled(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIGNUPS_ENABLED", "false")
    get_settings.cache_clear()
    resp = auth_client.post("/auth/google", json={"idToken": "stub", "inviteCode": "IRRELEVANT"})
    get_settings.cache_clear()
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_SIGNUPS_DISABLED"


def test_new_signup_blocked_at_capacity(
    auth_client: TestClient,
    fake_invite_code_repo: FakeInviteCodeRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIGNUP_CAPACITY", "0")
    get_settings.cache_clear()
    resp = auth_client.post("/auth/google", json={"idToken": "stub", "inviteCode": "IRRELEVANT"})
    get_settings.cache_clear()
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_SIGNUP_CAPACITY_REACHED"


def test_existing_user_login_unaffected_by_capacity(
    auth_client: TestClient,
    fake_invite_code_repo: FakeInviteCodeRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _login(auth_client, fake_invite_code_repo)  # capacity 가 넉넉할 때 먼저 가입
    monkeypatch.setenv("SIGNUP_CAPACITY", "0")
    get_settings.cache_clear()
    resp = auth_client.post("/auth/google", json={"idToken": "stub"})
    get_settings.cache_clear()
    assert resp.status_code == 200
