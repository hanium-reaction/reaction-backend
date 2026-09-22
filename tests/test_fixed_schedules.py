"""Fixed Schedules — 실 구현 (Issue #17, api-contract §19)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from reaction_backend.db.models.user import User
from reaction_backend.orchestrator.goal_structuring import fixed_schedules_to_busy
from reaction_backend.schemas.common import KST
from tests.conftest import FakeFixedScheduleRepo


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


def _post(client: TestClient, **overrides: object) -> Any:
    body: dict[str, object] = {
        "title": "알고리즘 수업",
        "daysOfWeek": ["mon", "wed"],
        "startTime": "09:00",
        "endTime": "10:30",
    }
    body.update(overrides)
    return client.post("/fixed-schedules", json=body)


# ── 입력 검증 (calendar-6) ───────────────────────────────────────────────


def test_title_over_200_chars_is_422_not_500(client: TestClient) -> None:
    """컬럼이 String(200) 이라 예전엔 DB 가 거절해 원인 모를 500('저장하지 못했어요')이었다."""
    resp = _post(client, title="가" * 201)
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"
    assert resp.json()["field"] == "title"
    assert _post(client, title="가" * 200).status_code == 201


def test_whitespace_only_title_is_rejected_and_titles_are_trimmed(client: TestClient) -> None:
    assert _post(client, title="   ").status_code == 422
    resp = _post(client, title="  데이터베이스  ")
    assert resp.status_code == 201
    assert resp.json()["title"] == "데이터베이스"


def test_duplicate_days_are_stored_once(client: TestClient) -> None:
    """['mon','mon'] 이 그대로 저장되면 '월·월' 로 그려진다."""
    resp = _post(client, daysOfWeek=["mon", "wed", "mon"])
    assert resp.status_code == 201
    assert resp.json()["daysOfWeek"] == ["mon", "wed"]


def test_patch_cannot_empty_the_title_or_the_days(client: TestClient) -> None:
    """빈 요일 = 아무것도 막지 않으면서 목록에만 남는 일정. '안 바꿈' 은 필드를 빼는 것이다."""
    created = _post(client).json()
    url = f"/fixed-schedules/{created['scheduleId']}"

    assert client.patch(url, json={"title": ""}).status_code == 422
    assert client.patch(url, json={"title": "   "}).status_code == 422
    empty_days = client.patch(url, json={"daysOfWeek": []})
    assert empty_days.status_code == 422
    assert empty_days.json()["field"] == "daysOfWeek"
    assert client.get("/fixed-schedules").json()[0]["title"] == "알고리즘 수업"


# ── 24:00 · 자정 넘김 (calendar-5 · journey-10) ─────────────────────────────


def test_until_midnight_is_accepted_and_blocks_the_rest_of_the_day(
    client: TestClient, fake_fixed_schedule_repo: FakeFixedScheduleRepo
) -> None:
    """ "밤 12시까지" 카페 알바 — 예전엔 '24:00' 을 형식 오류로 거절해 넣을 방법이 없었다."""
    resp = _post(client, title="카페 알바", daysOfWeek=["fri"], startTime="18:00", endTime="24:00")
    assert resp.status_code == 201, resp.text

    friday = date(2026, 9, 18)
    busy = fixed_schedules_to_busy(friday, list(fake_fixed_schedule_repo._items.values()))
    assert len(busy) == 1
    assert busy[0].interval.start == datetime.combine(friday, time(18, 0), tzinfo=KST)
    # 그날의 마지막 순간까지 — 23:59 에서 1분이 비지 않는다.
    assert busy[0].interval.end == datetime.combine(friday, time.max, tzinfo=KST)


def test_overnight_window_explains_how_to_enter_it(client: TestClient) -> None:
    """22:00–02:00 을 같은 요일로 접으면 금요일 새벽을 막고 토요일 새벽은 비운다 — 받지 않고,
    어떻게 넣으면 되는지를 말한다(예전엔 '시작이 종료보다 빨라야' 뿐이었다)."""
    resp = _post(
        client, title="편의점 야간", daysOfWeek=["fri"], startTime="22:00", endTime="02:00"
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["field"] == "startTime"
    assert "자정" in body["message"] and "나눠" in body["message"]


def test_equal_start_and_end_keeps_the_plain_message(client: TestClient) -> None:
    resp = _post(client, startTime="09:00", endTime="09:00")
    assert resp.status_code == 422
    assert resp.json()["message"] == "시작 시각은 종료 시각보다 빨라야 해요."


def test_bad_time_format_message_has_no_field_code(client: TestClient) -> None:
    """사용자에게 보이는 문장에 'endTime' 같은 내부 이름을 싣지 않는다 — 기계용은 `field` 에."""
    resp = _post(client, startTime="7:3x")
    assert resp.status_code == 422
    body = resp.json()
    assert body["field"] == "startTime"
    assert "startTime" not in body["message"] and "HH:MM" not in body["message"]
    assert "시작 시각" in body["message"]


# ── 겹침 409 (calendar-7) ────────────────────────────────────────────────


def test_double_tap_does_not_create_a_duplicate(client: TestClient) -> None:
    """느린 연결에서 [추가] 두 번 — 같은 수업이 두 줄 생겨 오늘 화면에도 두 번 보였다."""
    assert _post(client).status_code == 201
    second = _post(client)
    assert second.status_code == 409
    assert second.json()["code"] == "FIXED_SCHEDULE_OVERLAP"
    assert len(client.get("/fixed-schedules").json()) == 1


def test_overlap_is_per_weekday_and_touching_is_fine(client: TestClient) -> None:
    assert _post(client, daysOfWeek=["mon"], startTime="09:00", endTime="10:00").status_code == 201
    # 같은 요일·걸침 → 409
    assert _post(client, daysOfWeek=["mon"], startTime="09:30", endTime="11:00").status_code == 409
    # 다른 요일 같은 시각 → 괜찮다
    assert _post(client, daysOfWeek=["tue"], startTime="09:00", endTime="10:00").status_code == 201
    # 맞닿기만(10:00 끝 → 10:00 시작) → 괜찮다
    assert _post(client, daysOfWeek=["mon"], startTime="10:00", endTime="11:00").status_code == 201


def test_patch_into_an_overlap_is_409_but_title_only_edits_pass(
    client: TestClient, fake_fixed_schedule_repo: FakeFixedScheduleRepo
) -> None:
    """요일·시각을 바꿀 때만 겹침을 본다 — 검사가 생기기 전에 이미 겹쳐 저장된 일정도 이름은
    고칠 수 있어야 한다."""
    _post(client, daysOfWeek=["mon"], startTime="09:00", endTime="10:00")
    later = _post(client, daysOfWeek=["mon"], startTime="13:00", endTime="14:00").json()
    url = f"/fixed-schedules/{later['scheduleId']}"

    moved = client.patch(url, json={"startTime": "09:30"})
    assert moved.status_code == 409
    assert moved.json()["code"] == "FIXED_SCHEDULE_OVERLAP"
    # 자기 자신과는 겹치지 않는다
    assert client.patch(url, json={"startTime": "12:30"}).status_code == 200

    # 예전 데이터: 이미 겹쳐 저장된 일정 — 이름 고치기는 막지 않는다.
    legacy = next(s for s in fake_fixed_schedule_repo._items.values() if s.title == "알고리즘 수업")
    legacy.start_time, legacy.end_time = time(12, 0), time(13, 0)
    assert client.patch(url, json={"title": "자료구조 수업"}).status_code == 200


def test_overlap_check_runs_under_the_per_user_lock(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_fixed_schedule_repo: FakeFixedScheduleRepo,
) -> None:
    """검사와 저장 사이에 다른 요청이 끼면 둘 다 '겹침 없음' 을 본다 — 같은 lock 안이어야 한다."""
    from contextlib import asynccontextmanager

    from reaction_backend.api.routes import fixed_schedules as route

    events: list[str] = []

    @asynccontextmanager
    async def _lock(session: Any, user_id: Any, agent: str) -> AsyncIterator[None]:
        events.append(f"lock:{agent}")
        yield
        events.append("unlock")

    original_list = fake_fixed_schedule_repo.list_active
    original_create = fake_fixed_schedule_repo.create

    async def _list(user_id: Any) -> Any:
        events.append("list")
        return await original_list(user_id)

    async def _create(**kwargs: Any) -> Any:
        events.append("create")
        return await original_create(**kwargs)

    monkeypatch.setattr(route, "user_agent_lock", _lock)
    monkeypatch.setattr(fake_fixed_schedule_repo, "list_active", _list)
    monkeypatch.setattr(fake_fixed_schedule_repo, "create", _create)

    assert _post(client).status_code == 201
    assert events == ["lock:fixed_schedules", "list", "create", "unlock"]


async def test_until_midnight_round_trips_through_postgres(real_db_session: Any) -> None:
    """`24:00` 은 `time.max`(23:59:59.999999)로 저장된다 — Postgres `time` 이 마이크로초까지
    그대로 돌려줘야 스케줄러가 자정까지 막는다(잘려서 23:59 가 되면 1분이 빈다)."""
    import uuid

    from reaction_backend.repositories.fixed_schedule_repo import FixedScheduleRepo

    user_id = uuid.uuid4()
    real_db_session.add(User(id=user_id, email=f"fixed+{user_id}@test.local", name="고정 일정"))
    await real_db_session.flush()
    repo = FixedScheduleRepo(real_db_session)
    await repo.create(
        user_id=user_id,
        title="카페 알바",
        days_of_week=["fri"],
        start_time=time(18, 0),
        end_time=time.max,
    )
    real_db_session.expire_all()

    [stored] = await repo.list_active(user_id)
    assert stored.end_time == time.max
