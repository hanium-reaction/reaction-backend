"""Time Policies 스텁 (api-contract §5 / #3-C)."""

from fastapi.testclient import TestClient

from reaction_backend.api.mock.time_policies import DEMO_POLICIES


def test_list_policies(client: TestClient) -> None:
    resp = client.get("/time-policies")
    assert resp.status_code == 200
    assert len(resp.json()) == len(DEMO_POLICIES)


def test_create_policy(client: TestClient) -> None:
    resp = client.post(
        "/time-policies",
        json={"policyType": "lunch", "payload": {"startTime": "12:00", "endTime": "13:00"}},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["policyType"] == "lunch"
    assert body["isActive"] is True


def test_create_policy_rejects_bad_type(client: TestClient) -> None:
    resp = client.post("/time-policies", json={"policyType": "bogus", "payload": {}})
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_prefill_from_interview(client: TestClient) -> None:
    resp = client.post("/time-policies/prefill-from-interview")
    assert resp.status_code == 200
    assert len(resp.json()) == len(DEMO_POLICIES)


def test_update_policy(client: TestClient) -> None:
    resp = client.patch(f"/time-policies/{DEMO_POLICIES[0].policy_id}", json={"isActive": False})
    assert resp.status_code == 200
    assert resp.json()["isActive"] is False


def test_update_policy_not_found(client: TestClient) -> None:
    resp = client.patch("/time-policies/nonexistent", json={"isActive": False})
    assert resp.status_code == 404
    assert resp.json()["code"] == "POLICY_NOT_FOUND"


def test_delete_policy(client: TestClient) -> None:
    resp = client.delete(f"/time-policies/{DEMO_POLICIES[0].policy_id}")
    assert resp.status_code == 204
