"""Auth 스텁 (api-contract §2 / #3-B)."""

from fastapi.testclient import TestClient

from reaction_backend.api.mock.demo import DEMO_REFRESH_TOKEN, DEMO_USER


def test_google_login_returns_session(client: TestClient) -> None:
    resp = client.post("/auth/google", json={"idToken": "google-id-token"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accessToken"]
    assert body["refreshToken"]
    assert body["user"]["email"] == DEMO_USER.email
    assert body["user"]["onboardingState"] == DEMO_USER.onboarding_state


def test_google_login_rejects_empty_id_token(client: TestClient) -> None:
    resp = client.post("/auth/google", json={"idToken": ""})
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_refresh_with_valid_token(client: TestClient) -> None:
    resp = client.post("/auth/refresh", json={"refreshToken": DEMO_REFRESH_TOKEN})
    assert resp.status_code == 200
    assert resp.json()["accessToken"]


def test_refresh_with_invalid_token(client: TestClient) -> None:
    resp = client.post("/auth/refresh", json={"refreshToken": "bogus-token"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_TOKEN"


def test_logout_returns_204(client: TestClient) -> None:
    resp = client.post("/auth/logout", json={"refreshToken": DEMO_REFRESH_TOKEN})
    assert resp.status_code == 204


def test_me_returns_demo_user(client: TestClient) -> None:
    resp = client.get("/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == DEMO_USER.email
    assert body["onboardingState"] == DEMO_USER.onboarding_state
