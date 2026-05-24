"""Onboarding /status — 인증 + 상태머신 → 화면 매핑 (Issue #16)."""

from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient

from reaction_backend.api.deps import get_current_user
from reaction_backend.db.models.user import User


def _user_with_state(state: str) -> User:
    u = User()
    u.id = UUID("22222222-2222-4222-8222-222222222222")
    u.email = "x@reaction.local"
    u.name = "신규"
    u.timezone = "Asia/Seoul"
    u.onboarding_state = state
    u.tone_mode = None
    return u


def test_status_active_routes_to_main(client: TestClient) -> None:
    """기본 demo user(ACTIVE) → S10."""
    resp = client.get("/onboarding/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["currentState"] == "ACTIVE"
    assert body["suggestedNextScreen"] == "S10"


def test_status_welcome_routes_to_interview(unauthed_client: TestClient) -> None:
    """WELCOME → S02 (interview)."""
    unauthed_client.app.dependency_overrides[get_current_user] = lambda: _user_with_state("WELCOME")
    resp = unauthed_client.get("/onboarding/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["currentState"] == "WELCOME"
    assert body["suggestedNextScreen"] == "S02"


def test_status_interview_state_routes_to_S02(unauthed_client: TestClient) -> None:
    unauthed_client.app.dependency_overrides[get_current_user] = lambda: _user_with_state(
        "ONBOARDING_INTERVIEW"
    )
    body = unauthed_client.get("/onboarding/status").json()
    assert body["suggestedNextScreen"] == "S02"


def test_status_without_auth_returns_401(unauthed_client: TestClient) -> None:
    resp = unauthed_client.get("/onboarding/status")
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_TOKEN"
