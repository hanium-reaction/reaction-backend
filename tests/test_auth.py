"""Auth — Google OAuth + JWT 세션 실구현 (Issue #16).

`auth_client` fixture: repo/session 만 override, 인증은 실제 JWT 흐름.
stub 모드에서 verifier 가 고정 demo 클레임 반환 → FakeUserRepo 가 user 생성.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import DEMO_USER_UUID, FakeUserRepo, issue_helper_token


def test_google_login_creates_new_user(auth_client: TestClient) -> None:
    resp = auth_client.post("/auth/google", json={"idToken": "stub-id-token"})
    assert resp.status_code == 200
    body = resp.json()
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
    auth_client: TestClient, fake_user_repo: FakeUserRepo
) -> None:
    """같은 email 로 두 번 로그인 — 동일 user_id, onboarding_state 보존."""
    first = auth_client.post("/auth/google", json={"idToken": "stub"}).json()
    second = auth_client.post("/auth/google", json={"idToken": "stub"}).json()
    assert first["user"]["userId"] == second["user"]["userId"]
    assert len(fake_user_repo._by_email) == 1


def test_stub_device_token_creates_isolated_users(
    auth_client: TestClient, fake_user_repo: FakeUserRepo
) -> None:
    """`demo:<id>` — 브라우저별 격리 데모 계정 (테스터 충돌 방지)."""
    a = auth_client.post("/auth/google", json={"idToken": "demo:tester-one"}).json()
    b = auth_client.post("/auth/google", json={"idToken": "demo:tester-two"}).json()
    again = auth_client.post("/auth/google", json={"idToken": "demo:tester-one"}).json()

    assert a["user"]["email"] == "demo+tester-one@reaction.local"
    assert b["user"]["email"] == "demo+tester-two@reaction.local"
    assert a["user"]["userId"] != b["user"]["userId"]  # 서로 다른 유저
    assert again["user"]["userId"] == a["user"]["userId"]  # 같은 id 는 같은 유저
    assert len(fake_user_repo._by_email) == 2


def test_stub_plain_token_keeps_fixed_demo_account(auth_client: TestClient) -> None:
    """`demo:` 접두사가 아니면 종전대로 고정 demo 계정 — 시드 시나리오 계정 유지."""
    res = auth_client.post("/auth/google", json={"idToken": "anything-else"}).json()
    assert res["user"]["email"] == "demo@reaction.local"

    # 접두사만 있고 id 가 비면(정규화 후 빈 slug) 고정 계정으로 fallback
    edge = auth_client.post("/auth/google", json={"idToken": "demo:!!!"}).json()
    assert edge["user"]["email"] == "demo@reaction.local"


def test_refresh_returns_new_access(auth_client: TestClient) -> None:
    login = auth_client.post("/auth/google", json={"idToken": "stub"}).json()
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


def test_refresh_with_access_token_rejected(auth_client: TestClient) -> None:
    """access 토큰을 refresh 자리에 보내면 type mismatch → INVALID_TOKEN."""
    login = auth_client.post("/auth/google", json={"idToken": "stub"}).json()
    resp = auth_client.post(
        "/auth/refresh",
        json={"refreshToken": login["accessToken"]},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_TOKEN"


def test_logout_revokes_refresh(auth_client: TestClient) -> None:
    login = auth_client.post("/auth/google", json={"idToken": "stub"}).json()
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


def test_me_returns_profile_with_valid_token(auth_client: TestClient) -> None:
    login = auth_client.post("/auth/google", json={"idToken": "stub"}).json()
    access = login["accessToken"]
    resp = auth_client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "demo@reaction.local"
    assert body["onboardingState"] == "WELCOME"


def test_me_without_token_returns_401(auth_client: TestClient) -> None:
    resp = auth_client.get("/auth/me")
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


def test_me_with_refresh_token_rejected(auth_client: TestClient) -> None:
    """refresh 토큰을 access 자리에 보내면 type mismatch → INVALID_TOKEN."""
    login = auth_client.post("/auth/google", json={"idToken": "stub"}).json()
    refresh = login["refreshToken"]
    resp = auth_client.get("/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_TOKEN"
