"""습관 주간 카운터 정합성 — 체크가 엉뚱한 주·옛 목표·되돌릴 수 없는 값으로 남지 않게.

- goals-3: 방금 만든 습관의 이번 주 인스턴스 id 를 생성 응답에 싣는다(`currentInstanceId`).
- critic-8: 등록한 주는 남은 날만큼 목표를 줄인다(토요일에 '매일' → 2).
- critic-9: 지난 주 인스턴스로 온 체크는 이번 주로 옮기고, 이번 주 조회는 cron 전에도 채운다.
- goals-24: 잘못 누른 체크를 되돌린다(`/uncheck`, 0 아래로 안 내려감).
- review-5: 빈도를 바꾸면 이번 주 인스턴스의 목표도 바뀐다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from reaction_backend.db.models.habit import Habit
from reaction_backend.repositories import habit_repo
from reaction_backend.repositories.habit_repo import current_week_start_kst, week_target
from tests.conftest import DEMO_USER_UUID, FakeGoalRepo, FakeHabitInstanceRepo, FakeHabitRepo
from tests.test_mandala_tree_route import _goal, _seed_full_tree

_THIS_MONDAY = current_week_start_kst()


def _freeze_today(monkeypatch: pytest.MonkeyPatch, day: date) -> None:
    monkeypatch.setattr(habit_repo, "today_kst", lambda: day)


def _create(client: TestClient, *, freq: int = 3) -> dict[str, Any]:
    resp = client.post(
        "/habits",
        json={
            "title": "스트레칭",
            "category": "health",
            "frequencyPerWeek": freq,
            "minutesPerSession": 10,
            "timePreference": "evening",
            "priorityLevel": 2,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _seed_habit(repo: FakeHabitRepo, *, freq: int = 3, user_id: UUID = DEMO_USER_UUID) -> Habit:
    h = Habit()
    h.id = uuid4()
    h.user_id = user_id
    h.title = "물 마시기"
    h.category = "health"
    h.frequency_per_week = freq
    h.target_count = freq
    h.minutes_per_session = 5
    h.time_preference = "anytime"
    h.priority_level = 3
    h.goal_node_id = None
    h.archived_at = None
    repo.seed(h)
    return h


# ───────────────────────── 등록 주 목표(critic-8) ─────────────────────────


def test_week_target_prorates_only_the_creation_week() -> None:
    monday = date(2026, 9, 14)
    assert week_target(7, created_on=monday + timedelta(days=5), week_start=monday) == 2  # 토
    assert week_target(7, created_on=monday, week_start=monday) == 7  # 월
    assert week_target(3, created_on=monday + timedelta(days=3), week_start=monday) == 2  # 목
    assert week_target(1, created_on=monday + timedelta(days=6), week_start=monday) == 1  # 최소 1
    # 지난 주에 만든 습관은 이번 주 빈도 그대로.
    assert week_target(7, created_on=monday - timedelta(days=1), week_start=monday) == 7


def test_saturday_daily_habit_starts_with_a_reachable_target(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze_today(monkeypatch, _THIS_MONDAY + timedelta(days=5))
    habit = _create(client, freq=7)

    instances = client.get("/habit-instances").json()

    assert [i["targetCount"] for i in instances] == [2]
    assert habit["frequencyPerWeek"] == 7  # 습관 자체의 빈도는 그대로


def test_monday_habit_keeps_the_full_target(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze_today(monkeypatch, _THIS_MONDAY)
    _create(client, freq=7)
    assert client.get("/habit-instances").json()[0]["targetCount"] == 7


# ───────────────────── 생성 응답의 인스턴스 id(goals-3) ─────────────────────


def test_created_habit_can_be_checked_right_away(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze_today(monkeypatch, _THIS_MONDAY)
    habit = _create(client)
    instance_id = habit["currentInstanceId"]
    assert instance_id.startswith("hinst_")

    resp = client.post(f"/habit-instances/{instance_id}/check")

    assert resp.status_code == 200, resp.text
    assert resp.json()["doneCount"] == 1
    assert resp.json()["habitId"] == habit["habitId"]
    # 목록 응답은 원본이 `/habit-instances` 라 싣지 않는다.
    assert client.get("/habits").json()[0]["currentInstanceId"] is None


# ─────────────────────── 주 경계(critic-9) ───────────────────────


def test_check_on_last_weeks_instance_counts_for_this_week(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    habit = _seed_habit(fake_habit_repo, freq=3)
    last_week = fake_habit_instance_repo.seed_instance(
        habit.id, _THIS_MONDAY - timedelta(days=7), done=3, target=3
    )

    resp = client.post(f"/habit-instances/hinst_{last_week.id}/check")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["weekStart"] == _THIS_MONDAY.isoformat()
    assert body["doneCount"] == 1
    assert body["targetCount"] == 3
    assert last_week.done_count == 3  # 지난 주 기록은 그대로


def test_this_weeks_list_fills_instances_before_the_cron(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    """월요일 00:00~00:05(cron 전)에도 이번 주 체크 대상이 있다."""
    habit = _seed_habit(fake_habit_repo, freq=4)

    items = client.get("/habit-instances").json()

    assert [(i["habitId"], i["targetCount"], i["doneCount"]) for i in items] == [
        (f"habit_{habit.id}", 4, 0)
    ]
    # 다시 불러도 1행(멱등).
    assert len(client.get("/habit-instances").json()) == 1
    # 지난 주 조회는 읽기만 한다.
    past = (_THIS_MONDAY - timedelta(days=7)).isoformat()
    assert client.get("/habit-instances", params={"weekStart": past}).json() == []


# ─────────────────────── 체크 되돌리기(goals-24) ───────────────────────


def test_uncheck_undoes_one_check_and_stops_at_zero(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze_today(monkeypatch, _THIS_MONDAY)
    instance_id = _create(client)["currentInstanceId"]
    client.post(f"/habit-instances/{instance_id}/check")

    first = client.post(f"/habit-instances/{instance_id}/uncheck")
    again = client.post(f"/habit-instances/{instance_id}/uncheck")

    assert first.status_code == 200, first.text
    assert first.json()["doneCount"] == 0
    assert again.status_code == 200
    assert again.json()["doneCount"] == 0


def test_uncheck_other_users_instance_is_404(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    other = _seed_habit(fake_habit_repo, user_id=uuid4())
    inst = fake_habit_instance_repo.seed_instance(other.id, _THIS_MONDAY, done=1, target=3)

    resp = client.post(f"/habit-instances/hinst_{inst.id}/uncheck")

    assert resp.status_code == 404
    assert resp.json()["code"] == "HABIT_NOT_FOUND"
    assert inst.done_count == 1


# ─────────────────────── 빈도 변경 동기화(review-5) ───────────────────────


def test_changing_frequency_updates_this_weeks_target(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze_today(monkeypatch, _THIS_MONDAY)
    habit = _create(client, freq=5)
    instance_id = habit["currentInstanceId"]
    for _ in range(4):
        client.post(f"/habit-instances/{instance_id}/check")

    resp = client.patch(f"/habits/{habit['habitId']}", json={"frequencyPerWeek": 2})

    assert resp.status_code == 200, resp.text
    [inst] = client.get("/habit-instances").json()
    assert inst["targetCount"] == 2
    assert inst["doneCount"] == 2  # 이미 한 횟수는 새 목표에서 멈춘다


def test_title_only_patch_leaves_this_weeks_target(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze_today(monkeypatch, _THIS_MONDAY)
    habit = _create(client, freq=5)
    client.patch(f"/habits/{habit['habitId']}", json={"title": "스트레칭 10분"})
    assert client.get("/habit-instances").json()[0]["targetCount"] == 5


def test_mandala_repeat_cell_returns_its_instance_and_prorates(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_goal_repo: FakeGoalRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    """반복형 전환도 `POST /habits` 와 같다 — 인스턴스 id 를 싣고, 등록 주는 남은 날만큼."""
    _freeze_today(monkeypatch, _THIS_MONDAY + timedelta(days=5))
    ids = _seed_full_tree(fake_goal_repo, _goal())
    body = {"frequencyPerWeek": 7, "minutesPerSession": 20}

    first = client.post(f"/goals/mandala/nodes/node_{ids['leaf2'].id}/habit", json=body)
    again = client.post(f"/goals/mandala/nodes/node_{ids['leaf2'].id}/habit", json=body)

    assert first.status_code == 201, first.text
    instance_id = first.json()["currentInstanceId"]
    assert instance_id.startswith("hinst_")
    assert again.json()["currentInstanceId"] == instance_id  # 멱등 경로도 같은 id
    inst = fake_habit_instance_repo._items[UUID(instance_id[len("hinst_") :])]
    assert inst.target_count == 2
