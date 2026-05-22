"""Onboarding 스텁 (api-contract §3 / #3-B)."""

from fastapi.testclient import TestClient

from reaction_backend.api.mock.demo import DEMO_USER


def test_status_returns_state_and_screen(client: TestClient) -> None:
    resp = client.get("/onboarding/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["currentState"] == DEMO_USER.onboarding_state
    # demo user 는 ACTIVE → 메인 화면(S10)
    assert body["suggestedNextScreen"] == "S10"
