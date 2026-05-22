"""Fixed Schedules 스텁 (api-contract §19 / #3-C)."""

from fastapi.testclient import TestClient

from reaction_backend.api.mock.fixed_schedules import DEMO_FIXED_SCHEDULES


def test_list_schedules(client: TestClient) -> None:
    resp = client.get("/fixed-schedules")
    assert resp.status_code == 200
    assert len(resp.json()) == len(DEMO_FIXED_SCHEDULES)


def test_create_schedule(client: TestClient) -> None:
    resp = client.post(
        "/fixed-schedules",
        json={
            "title": "데이터베이스 수업",
            "daysOfWeek": ["tue", "thu"],
            "startTime": "13:00",
            "endTime": "14:30",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["title"] == "데이터베이스 수업"


def test_create_schedule_rejects_empty_title(client: TestClient) -> None:
    resp = client.post(
        "/fixed-schedules",
        json={"title": "", "daysOfWeek": ["mon"], "startTime": "09:00", "endTime": "10:00"},
    )
    assert resp.status_code == 422


def test_update_schedule(client: TestClient) -> None:
    resp = client.patch(
        f"/fixed-schedules/{DEMO_FIXED_SCHEDULES[0].schedule_id}", json={"title": "수정된 수업"}
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "수정된 수업"


def test_update_schedule_not_found(client: TestClient) -> None:
    resp = client.patch("/fixed-schedules/nonexistent", json={"title": "x"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "FIXED_SCHEDULE_NOT_FOUND"


def test_delete_schedule(client: TestClient) -> None:
    resp = client.delete(f"/fixed-schedules/{DEMO_FIXED_SCHEDULES[0].schedule_id}")
    assert resp.status_code == 204
