"""Habits + Habit Instances 스텁 (api-contract §7 / #3-D)."""

from fastapi.testclient import TestClient

from reaction_backend.api.mock.habits import DEMO_HABIT_INSTANCES, DEMO_HABITS


def test_list_habits(client: TestClient) -> None:
    resp = client.get("/habits")
    assert resp.status_code == 200
    assert len(resp.json()) == len(DEMO_HABITS)


def test_create_habit(client: TestClient) -> None:
    resp = client.post(
        "/habits",
        json={
            "title": "독서 30분",
            "category": "자기계발",
            "frequencyPerWeek": 4,
            "minutesPerSession": 30,
            "timePreference": "evening",
            "priorityLevel": 2,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "독서 30분"
    assert body["frequencyPerWeek"] == 4


def test_create_habit_rejects_bad_time_preference(client: TestClient) -> None:
    resp = client.post(
        "/habits",
        json={
            "title": "x",
            "category": "y",
            "frequencyPerWeek": 3,
            "minutesPerSession": 10,
            "timePreference": "midnight",
            "priorityLevel": 1,
        },
    )
    assert resp.status_code == 422


def test_update_habit(client: TestClient) -> None:
    resp = client.patch(f"/habits/{DEMO_HABITS[0].habit_id}", json={"frequencyPerWeek": 5})
    assert resp.status_code == 200
    assert resp.json()["frequencyPerWeek"] == 5


def test_update_habit_not_found(client: TestClient) -> None:
    resp = client.patch("/habits/nonexistent", json={"title": "x"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "HABIT_NOT_FOUND"


def test_delete_habit(client: TestClient) -> None:
    resp = client.delete(f"/habits/{DEMO_HABITS[0].habit_id}")
    assert resp.status_code == 204


def test_list_habit_instances(client: TestClient) -> None:
    resp = client.get("/habit-instances", params={"weekStart": "2026-05-18"})
    assert resp.status_code == 200
    assert len(resp.json()) == len(DEMO_HABIT_INSTANCES)


def test_check_instance(client: TestClient) -> None:
    initial = DEMO_HABIT_INSTANCES[0]
    resp = client.post(f"/habit-instances/{initial.instance_id}/check")
    assert resp.status_code == 200
    assert resp.json()["doneCount"] == initial.done_count + 1


def test_check_instance_not_found(client: TestClient) -> None:
    resp = client.post("/habit-instances/nonexistent/check")
    assert resp.status_code == 404
    assert resp.json()["code"] == "HABIT_NOT_FOUND"
