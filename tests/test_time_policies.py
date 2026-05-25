"""Time Policies — 실 구현 (Issue #17, api-contract §5)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from reaction_backend.db.models.user import User


def test_list_empty_when_no_policies(client: TestClient) -> None:
    resp = client.get("/time-policies")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_policy_returns_201(client: TestClient) -> None:
    resp = client.post(
        "/time-policies",
        json={"policyType": "lunch", "payload": {"startTime": "12:00", "endTime": "13:00"}},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["policyType"] == "lunch"
    assert body["isActive"] is True
    assert body["policyId"].startswith("policy_")


def test_create_policy_rejects_bad_type(client: TestClient) -> None:
    resp = client.post("/time-policies", json={"policyType": "bogus", "payload": {}})
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_list_after_create(client: TestClient) -> None:
    client.post(
        "/time-policies",
        json={"policyType": "sleep", "payload": {"startTime": "23:00", "endTime": "07:00"}},
    )
    resp = client.get("/time-policies")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["policyType"] == "sleep"


def test_prefill_returns_default_when_no_interview_answers(client: TestClient) -> None:
    """interview 답 없음(_FakeSession이 빈 결과) → sleep/break_min/late_night_block 3개 default."""
    resp = client.post("/time-policies/prefill-from-interview")
    assert resp.status_code == 200
    items = resp.json()
    types = [c["policyType"] for c in items]
    assert "sleep" in types
    assert "break_min" in types
    assert "late_night_block" in types
    # DB 미저장 — 모든 policyId 가 prefill prefix
    assert all(c["policyId"].startswith("policy_prefill_") for c in items)


def test_update_policy_is_active(client: TestClient) -> None:
    created = client.post(
        "/time-policies",
        json={"policyType": "break_min", "payload": {"minMinutes": 15}},
    ).json()
    resp = client.patch(f"/time-policies/{created['policyId']}", json={"isActive": False})
    assert resp.status_code == 200
    assert resp.json()["isActive"] is False


def test_update_policy_not_found(client: TestClient) -> None:
    resp = client.patch(
        "/time-policies/policy_11111111-1111-4111-8111-999999999999",
        json={"isActive": False},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "POLICY_NOT_FOUND"


def test_update_policy_not_found_bad_prefix(client: TestClient) -> None:
    resp = client.patch("/time-policies/bogus-id", json={"isActive": False})
    assert resp.status_code == 404
    assert resp.json()["code"] == "POLICY_NOT_FOUND"


def test_delete_policy(client: TestClient) -> None:
    created = client.post(
        "/time-policies",
        json={"policyType": "custom", "payload": {"note": "x"}},
    ).json()
    resp = client.delete(f"/time-policies/{created['policyId']}")
    assert resp.status_code == 204
    # soft delete — list 에서 빠짐
    assert client.get("/time-policies").json() == []


def test_create_advances_onboarding_state(client: TestClient, demo_user_orm: User) -> None:
    """ONBOARDING_POLICIES → ONBOARDING_FIRST_PLAN 멱등 전이."""
    demo_user_orm.onboarding_state = "ONBOARDING_POLICIES"
    client.post(
        "/time-policies",
        json={"policyType": "lunch", "payload": {"startTime": "12:00", "endTime": "13:00"}},
    )
    assert demo_user_orm.onboarding_state == "ONBOARDING_FIRST_PLAN"


def test_create_does_not_regress_state(client: TestClient, demo_user_orm: User) -> None:
    """이미 ACTIVE 사용자는 advance no-op."""
    demo_user_orm.onboarding_state = "ACTIVE"
    client.post(
        "/time-policies",
        json={"policyType": "lunch", "payload": {"startTime": "12:00", "endTime": "13:00"}},
    )
    assert demo_user_orm.onboarding_state == "ACTIVE"
