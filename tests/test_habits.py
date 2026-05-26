"""Habits + Habit instances — 실 구현 (Issue #22, api-contract §7)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from reaction_backend.repositories.habit_repo import current_week_start_kst


def _new_habit(client: TestClient, *, title: str = "운동", freq: int = 3) -> dict[str, Any]:
    resp = client.post(
        "/habits",
        json={
            "title": title,
            "category": "health",
            "frequencyPerWeek": freq,
            "minutesPerSession": 30,
            "timePreference": "morning",
            "priorityLevel": 2,
        },
    )
    assert resp.status_code == 201, resp.json()
    return resp.json()


def test_list_empty(client: TestClient) -> None:
    resp = client.get("/habits")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_habit(client: TestClient) -> None:
    body = _new_habit(client)
    assert body["title"] == "운동"
    assert body["frequencyPerWeek"] == 3
    assert body["habitId"].startswith("habit_")


def test_create_rejects_bad_frequency_high(client: TestClient) -> None:
    resp = client.post(
        "/habits",
        json={
            "title": "x",
            "category": "health",
            "frequencyPerWeek": 8,
            "minutesPerSession": 30,
            "timePreference": "morning",
            "priorityLevel": 2,
        },
    )
    assert resp.status_code == 422


def test_create_rejects_bad_frequency_zero(client: TestClient) -> None:
    resp = client.post(
        "/habits",
        json={
            "title": "x",
            "category": "health",
            "frequencyPerWeek": 0,
            "minutesPerSession": 30,
            "timePreference": "morning",
            "priorityLevel": 2,
        },
    )
    assert resp.status_code == 422


def test_create_auto_creates_this_week_instance(client: TestClient) -> None:
    """POST /habits 시 이번 주 instance 자동 생성 — cron 도입 전 임시."""
    habit = _new_habit(client, title="물 마시기", freq=5)
    ws = current_week_start_kst().isoformat()
    resp = client.get("/habit-instances", params={"weekStart": ws})
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["habitId"] == habit["habitId"]
    assert items[0]["targetCount"] == 5
    assert items[0]["doneCount"] == 0
    assert items[0]["instanceId"].startswith("hinst_")


def test_update_title(client: TestClient) -> None:
    created = _new_habit(client)
    resp = client.patch(f"/habits/{created['habitId']}", json={"title": "수정된 운동"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "수정된 운동"


def test_update_frequency(client: TestClient) -> None:
    created = _new_habit(client, freq=3)
    resp = client.patch(f"/habits/{created['habitId']}", json={"frequencyPerWeek": 5})
    assert resp.status_code == 200
    assert resp.json()["frequencyPerWeek"] == 5


def test_update_rejects_bad_frequency(client: TestClient) -> None:
    created = _new_habit(client)
    resp = client.patch(f"/habits/{created['habitId']}", json={"frequencyPerWeek": 8})
    assert resp.status_code == 422


def test_update_not_found(client: TestClient) -> None:
    resp = client.patch(
        "/habits/habit_99999999-9999-4999-8999-999999999999",
        json={"title": "x"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "HABIT_NOT_FOUND"


def test_update_bad_id_format(client: TestClient) -> None:
    resp = client.patch("/habits/nonexistent", json={"title": "x"})
    assert resp.status_code == 404


def test_delete_habit(client: TestClient) -> None:
    created = _new_habit(client)
    resp = client.delete(f"/habits/{created['habitId']}")
    assert resp.status_code == 204
    assert client.get("/habits").json() == []


def test_list_instances_empty_other_week(client: TestClient) -> None:
    """다른 주 — instance 없음."""
    _new_habit(client)
    resp = client.get("/habit-instances", params={"weekStart": "2020-01-06"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_instances_defaults_to_this_week(client: TestClient) -> None:
    """weekStart 누락 시 이번 주 (KST 월요일)."""
    _new_habit(client)
    resp = client.get("/habit-instances")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["weekStart"] == current_week_start_kst().isoformat()


def test_list_instances_bad_week_format(client: TestClient) -> None:
    resp = client.get("/habit-instances", params={"weekStart": "not-a-date"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_check_increments_done_count(client: TestClient) -> None:
    _new_habit(client)
    inst = client.get("/habit-instances").json()[0]
    resp = client.post(f"/habit-instances/{inst['instanceId']}/check")
    assert resp.status_code == 200
    assert resp.json()["doneCount"] == 1


def test_check_increments_repeated(client: TestClient) -> None:
    _new_habit(client)
    inst = client.get("/habit-instances").json()[0]
    for _ in range(3):
        client.post(f"/habit-instances/{inst['instanceId']}/check")
    final = client.get("/habit-instances").json()[0]
    assert final["doneCount"] == 3


def test_check_not_found(client: TestClient) -> None:
    resp = client.post("/habit-instances/hinst_99999999-9999-4999-8999-999999999999/check")
    assert resp.status_code == 404
    assert resp.json()["code"] == "HABIT_NOT_FOUND"


def test_check_bad_id_format(client: TestClient) -> None:
    resp = client.post("/habit-instances/nonexistent/check")
    assert resp.status_code == 404
