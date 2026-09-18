"""Habit Penalty — #21-C 슬라이스 (S22, api-contract §13).

3주 연속 미달(50% 미만) 감지 → 빈도 재설계 제안/수락. Idempotency-Key 필수(accept).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from reaction_backend.db.models.habit import Habit
from reaction_backend.db.models.habit_instance import HabitInstance
from reaction_backend.orchestrator.habit_penalty import evaluate_penalty
from reaction_backend.repositories.habit_repo import current_week_start_kst
from tests.conftest import DEMO_USER_UUID, FakeHabitInstanceRepo, FakeHabitRepo

# 직전 완료 주(월요일) — 라우트의 _last_completed_monday() 와 동일 기준.
REF = current_week_start_kst() - timedelta(days=7)
_HID = uuid4()


def _inst(week_start: date, done: int, target: int) -> HabitInstance:
    i = HabitInstance()
    i.id = uuid4()
    i.habit_id = _HID
    i.week_start = week_start
    i.done_count = done
    i.target_count = target
    return i


def _habit(*, freq: int = 5, title: str = "운동") -> Habit:
    h = Habit()
    h.id = uuid4()
    h.user_id = DEMO_USER_UUID
    h.title = title
    h.category = "health"
    h.frequency_per_week = freq
    h.target_count = freq
    h.minutes_per_session = 30
    h.time_preference = "morning"
    h.priority_level = 3
    h.archived_at = None
    h.consecutive_miss_weeks = 0
    h.last_penalty_evaluated_at = None
    h.last_penalty_decision = None
    return h


def _seed_3_weeks(
    inst_repo: FakeHabitInstanceRepo, habit: Habit, *, done: int, target: int
) -> None:
    for offset in range(3):
        inst_repo.seed_instance(
            habit.id, REF - timedelta(days=7 * offset), done=done, target=target
        )


# ───────────────────────── 순수 감지 ─────────────────────────

_W = date(2026, 6, 15)  # 월요일


def test_eval_eligible_three_weeks_below_half() -> None:
    desc = [
        _inst(_W, 1, 5),
        _inst(_W - timedelta(days=7), 1, 5),
        _inst(_W - timedelta(days=14), 2, 5),
    ]
    ev = evaluate_penalty(desc, current_frequency=5)
    assert ev is not None
    assert ev.suggested_frequency < 5
    assert len(ev.recent) == 3


def test_eval_needs_three_weeks() -> None:
    assert evaluate_penalty([_inst(_W, 1, 5), _inst(_W - timedelta(days=7), 1, 5)], 5) is None


def test_eval_not_consecutive() -> None:
    desc = [
        _inst(_W, 1, 5),
        _inst(_W - timedelta(days=7), 1, 5),
        _inst(_W - timedelta(days=21), 1, 5),
    ]
    assert evaluate_penalty(desc, 5) is None


def test_eval_one_week_above_half_blocks() -> None:
    desc = [
        _inst(_W, 3, 5),
        _inst(_W - timedelta(days=7), 1, 5),
        _inst(_W - timedelta(days=14), 1, 5),
    ]
    assert evaluate_penalty(desc, 5) is None  # 3/5 = 60% ≥ 50%


def test_eval_suggested_clamped_below_current() -> None:
    # 평균 달성(2)이 현재 빈도(2) 이상이어도 최소 1 감소 보장
    desc = [
        _inst(_W, 2, 5),
        _inst(_W - timedelta(days=7), 2, 5),
        _inst(_W - timedelta(days=14), 2, 5),
    ]
    ev = evaluate_penalty(desc, current_frequency=2)
    assert ev is not None
    assert ev.suggested_frequency == 1
    assert ev.total_done == 6


def test_eval_weekly_one_habit_has_nothing_to_suggest() -> None:
    """주 1회 습관은 더 줄일 빈도가 없다 — "주 1회 → 1회" 카드를 만들지 않는다."""
    desc = [
        _inst(_W, 0, 1),
        _inst(_W - timedelta(days=7), 0, 1),
        _inst(_W - timedelta(days=14), 0, 1),
    ]
    assert evaluate_penalty(desc, current_frequency=1) is None


def test_eval_rounds_average_to_suggestion() -> None:
    desc = [
        _inst(_W, 2, 5),
        _inst(_W - timedelta(days=7), 2, 5),
        _inst(_W - timedelta(days=14), 1, 3),
    ]
    ev = evaluate_penalty(desc, current_frequency=3)
    assert ev is not None
    assert ev.suggested_frequency == 2  # 평균 1.67 → 2


# ───────────────────────── GET /reviews/habit-penalty ─────────────────────────


def test_list_candidates(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    failing = _habit(freq=5, title="러닝")
    fake_habit_repo.seed(failing)
    _seed_3_weeks(fake_habit_instance_repo, failing, done=1, target=5)
    # 정상 습관 — 후보 아님
    ok = _habit(freq=5, title="독서")
    fake_habit_repo.seed(ok)
    for offset in range(3):
        fake_habit_instance_repo.seed_instance(
            ok.id, REF - timedelta(days=7 * offset), done=4, target=5
        )

    resp = client.get("/reviews/habit-penalty")
    assert resp.status_code == 200
    candidates = resp.json()["candidates"]
    assert len(candidates) == 1
    c = candidates[0]
    assert c["habitId"] == f"habit_{failing.id}"
    assert c["currentFrequency"] == 5
    assert c["suggestedFrequency"] < 5
    assert len(c["recentWeeks"]) == 3
    assert "주" in c["message"]


def test_list_requires_auth(unauthed_client: TestClient) -> None:
    assert unauthed_client.get("/reviews/habit-penalty").status_code == 401


# ───────────────────────── POST .../accept ─────────────────────────


def _accept(client: TestClient, habit_id: str, *, key: str | None = "k-1") -> Any:
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post(f"/reviews/habit-penalty/{habit_id}/accept", headers=headers)


def test_accept_reduces_frequency(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    habit = _habit(freq=5)
    fake_habit_repo.seed(habit)
    _seed_3_weeks(fake_habit_instance_repo, habit, done=1, target=5)

    resp = _accept(client, f"habit_{habit.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["previousFrequency"] == 5
    assert body["newFrequency"] < 5
    assert habit.frequency_per_week == body["newFrequency"]
    assert habit.last_penalty_decision == "accepted"


def test_accept_requires_idempotency_key(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    habit = _habit(freq=5)
    fake_habit_repo.seed(habit)
    _seed_3_weeks(fake_habit_instance_repo, habit, done=1, target=5)
    resp = _accept(client, f"habit_{habit.id}", key=None)
    assert resp.status_code == 400
    assert resp.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_accept_same_key_replays(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    habit = _habit(freq=5)
    fake_habit_repo.seed(habit)
    _seed_3_weeks(fake_habit_instance_repo, habit, done=1, target=5)
    first = _accept(client, f"habit_{habit.id}", key="same")
    second = _accept(client, f"habit_{habit.id}", key="same")
    assert first.json() == second.json()  # 캐시 재생


def test_accept_twice_different_key_not_eligible(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    habit = _habit(freq=5)
    fake_habit_repo.seed(habit)
    _seed_3_weeks(fake_habit_instance_repo, habit, done=1, target=5)
    assert _accept(client, f"habit_{habit.id}", key="a").status_code == 200
    # 같은 사이클 재수락 — 이미 결정됨
    resp = _accept(client, f"habit_{habit.id}", key="b")
    assert resp.status_code == 422
    assert resp.json()["code"] == "HABIT_PENALTY_NOT_ELIGIBLE"


def test_accept_not_eligible_when_above_half(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    habit = _habit(freq=5)
    fake_habit_repo.seed(habit)
    _seed_3_weeks(fake_habit_instance_repo, habit, done=4, target=5)
    resp = _accept(client, f"habit_{habit.id}", key="x")
    assert resp.status_code == 422
    assert resp.json()["code"] == "HABIT_PENALTY_NOT_ELIGIBLE"


def test_accept_habit_not_found(client: TestClient) -> None:
    resp = _accept(client, f"habit_{uuid4()}", key="x")
    assert resp.status_code == 404
    assert resp.json()["code"] == "HABIT_NOT_FOUND"


def test_accept_bad_habit_id(client: TestClient) -> None:
    resp = _accept(client, "not-a-habit", key="x")
    assert resp.status_code == 404
    assert resp.json()["code"] == "HABIT_NOT_FOUND"


def test_weekly_one_habit_gets_no_candidate_and_accept_is_not_eligible(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    habit = _habit(freq=1)
    fake_habit_repo.seed(habit)
    _seed_3_weeks(fake_habit_instance_repo, habit, done=0, target=1)

    assert client.get("/reviews/habit-penalty").json()["candidates"] == []
    resp = _accept(client, f"habit_{habit.id}", key="w1")
    assert resp.status_code == 422
    assert resp.json()["code"] == "HABIT_PENALTY_NOT_ELIGIBLE"
    assert habit.frequency_per_week == 1


def test_candidate_message_states_a_whole_number_total(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    habit = _habit(freq=5)
    fake_habit_repo.seed(habit)
    for offset, done in enumerate((0, 1, 1)):
        fake_habit_instance_repo.seed_instance(
            habit.id, REF - timedelta(days=7 * offset), done=done, target=5
        )

    message = client.get("/reviews/habit-penalty").json()["candidates"][0]["message"]
    assert "2회를 해냈어요" in message
    assert "평균" not in message
    assert re.search(r"\d\.\d", message) is None  # "0.67회" 같은 소수가 없다


def test_accept_updates_this_weeks_target(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    """빈도 줄이기를 수락하면 이번 주 카드도 새 목표로 — 예전엔 0/5 로 남았다."""
    habit = _habit(freq=5)
    fake_habit_repo.seed(habit)
    _seed_3_weeks(fake_habit_instance_repo, habit, done=1, target=5)
    this_week = fake_habit_instance_repo.seed_instance(
        habit.id, current_week_start_kst(), done=0, target=5
    )

    resp = _accept(client, f"habit_{habit.id}", key="sync")

    assert resp.status_code == 200, resp.text
    assert this_week.target_count == resp.json()["newFrequency"]
