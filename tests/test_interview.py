"""Interview 스텁 (api-contract §4 / #3-B)."""

from fastapi.testclient import TestClient

from reaction_backend.api.mock.interview import DEMO_SESSION_ID, SLOT_CATALOG


def test_start_session(client: TestClient) -> None:
    resp = client.post("/interview/sessions")
    assert resp.status_code == 201
    body = resp.json()
    assert body["sessionId"] == DEMO_SESSION_ID
    assert body["currentQuestion"]["slotKey"]
    assert isinstance(body["ambiguityScore"], int)


def test_slot_catalog(client: TestClient) -> None:
    resp = client.get("/interview/slot-catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == len(SLOT_CATALOG)
    assert set(body[0]) == {"slotKey", "label", "answerType", "isRequired", "category"}


def test_get_session_valid(client: TestClient) -> None:
    resp = client.get(f"/interview/sessions/{DEMO_SESSION_ID}")
    assert resp.status_code == 200
    assert resp.json()["sessionId"] == DEMO_SESSION_ID


def test_get_session_not_found(client: TestClient) -> None:
    resp = client.get("/interview/sessions/nonexistent")
    assert resp.status_code == 404
    assert resp.json()["code"] == "INTERVIEW_SESSION_NOT_FOUND"


def test_submit_answer(client: TestClient) -> None:
    resp = client.post(
        f"/interview/sessions/{DEMO_SESSION_ID}/answers",
        json={"slotKey": "goals.list", "value": ["캡스톤", "토익"], "clientTurn": 1},
    )
    assert resp.status_code == 200
    assert resp.json()["sessionId"] == DEMO_SESSION_ID


def test_submit_answer_invalid_session(client: TestClient) -> None:
    resp = client.post(
        "/interview/sessions/nonexistent/answers",
        json={"slotKey": "goals.list", "value": "캡스톤", "clientTurn": 1},
    )
    assert resp.status_code == 404


def test_next_question(client: TestClient) -> None:
    resp = client.post(f"/interview/sessions/{DEMO_SESSION_ID}/next-question")
    assert resp.status_code == 200
    assert resp.json()["currentQuestion"]["slotKey"]


def test_finish_session(client: TestClient) -> None:
    resp = client.post(f"/interview/sessions/{DEMO_SESSION_ID}/finish")
    assert resp.status_code == 200
    body = resp.json()
    assert body["endReason"] == "early_user"
    assert body["currentQuestion"] is None
