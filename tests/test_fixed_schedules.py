"""Fixed Schedules — 실 구현 (Issue #17, api-contract §19)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from reaction_backend.db.models.user import User


def test_list_empty_when_no_schedules(client: TestClient) -> None:
    resp = client.get("/fixed-schedules")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_schedule_returns_201(client: TestClient) -> None:
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
    body = resp.json()
    assert body["title"] == "데이터베이스 수업"
    assert body["scheduleId"].startswith("fixed_")
    assert body["startTime"] == "13:00"
    assert body["endTime"] == "14:30"


def test_create_rejects_empty_title(client: TestClient) -> None:
    resp = client.post(
        "/fixed-schedules",
        json={"title": "", "daysOfWeek": ["mon"], "startTime": "09:00", "endTime": "10:00"},
    )
    assert resp.status_code == 422


def test_create_rejects_bad_day(client: TestClient) -> None:
    resp = client.post(
        "/fixed-schedules",
        json={
            "title": "x",
            "daysOfWeek": ["monday"],
            "startTime": "09:00",
            "endTime": "10:00",
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "COMMON_VALIDATION_ERROR"
    assert body["field"] == "daysOfWeek"


def test_create_rejects_inverted_time_window(client: TestClient) -> None:
    resp = client.post(
        "/fixed-schedules",
        json={
            "title": "x",
            "daysOfWeek": ["mon"],
            "startTime": "10:00",
            "endTime": "09:00",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_list_after_create(client: TestClient) -> None:
    client.post(
        "/fixed-schedules",
        json={
            "title": "알바",
            "daysOfWeek": ["sat", "sun"],
            "startTime": "10:00",
            "endTime": "16:00",
        },
    )
    resp = client.get("/fixed-schedules")
    items = resp.json()
    assert len(items) == 1
    assert items[0]["title"] == "알바"


def test_update_schedule(client: TestClient) -> None:
    created = client.post(
        "/fixed-schedules",
        json={
            "title": "수업",
            "daysOfWeek": ["mon"],
            "startTime": "09:00",
            "endTime": "10:00",
        },
    ).json()
    resp = client.patch(
        f"/fixed-schedules/{created['scheduleId']}",
        json={"title": "수정된 수업"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "수정된 수업"


def test_update_schedule_not_found(client: TestClient) -> None:
    resp = client.patch(
        "/fixed-schedules/fixed_11111111-1111-4111-8111-999999999999",
        json={"title": "x"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "FIXED_SCHEDULE_NOT_FOUND"


def test_update_schedule_not_found_bad_prefix(client: TestClient) -> None:
    resp = client.patch("/fixed-schedules/nonexistent", json={"title": "x"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "FIXED_SCHEDULE_NOT_FOUND"


def test_delete_schedule(client: TestClient) -> None:
    created = client.post(
        "/fixed-schedules",
        json={
            "title": "x",
            "daysOfWeek": ["mon"],
            "startTime": "09:00",
            "endTime": "10:00",
        },
    ).json()
    resp = client.delete(f"/fixed-schedules/{created['scheduleId']}")
    assert resp.status_code == 204
    assert client.get("/fixed-schedules").json() == []


def test_create_advances_onboarding_state(client: TestClient, demo_user_orm: User) -> None:
    """ONBOARDING_CALENDAR → ONBOARDING_POLICIES 멱등 전이."""
    demo_user_orm.onboarding_state = "ONBOARDING_CALENDAR"
    client.post(
        "/fixed-schedules",
        json={
            "title": "x",
            "daysOfWeek": ["mon"],
            "startTime": "09:00",
            "endTime": "10:00",
        },
    )
    assert demo_user_orm.onboarding_state == "ONBOARDING_POLICIES"


def test_create_from_manual_schedule_state(client: TestClient, demo_user_orm: User) -> None:
    """ONBOARDING_MANUAL_SCHEDULE → ONBOARDING_POLICIES 전이도 OK."""
    demo_user_orm.onboarding_state = "ONBOARDING_MANUAL_SCHEDULE"
    client.post(
        "/fixed-schedules",
        json={
            "title": "x",
            "daysOfWeek": ["mon"],
            "startTime": "09:00",
            "endTime": "10:00",
        },
    )
    assert demo_user_orm.onboarding_state == "ONBOARDING_POLICIES"
