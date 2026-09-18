"""Habit Penalty — #21-C 슬라이스 (S22, api-contract §13).

3주 연속 미달(50% 미만) 감지 → 빈도 재설계 제안/수락. Idempotency-Key 필수(accept).
"""

from __future__ import annotations

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
    # 평균 달성이 현재 빈도 이상이어도 최소 1 감소 보장
    desc = [
        _inst(_W, 1, 3),
        _inst(_W - timedelta(days=7), 1, 3),
        _inst(_W - timedelta(days=14), 1, 3),
    ]
    ev = evaluate_penalty(desc, current_frequency=1)
    assert ev is not None
    assert ev.suggested_frequency == 1  # max(1, 1-1)=1 floor


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


# ───────────────────────── POST .../reject ('지금대로 유지') ─────────────────────────


def _reject(client: TestClient, habit_id: str) -> Any:
    return client.post(f"/reviews/habit-penalty/{habit_id}/reject")


def test_reject_keeps_frequency_and_hides_the_candidate(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    """'지금대로 유지' 뒤에는 같은 제안이 다시 뜨지 않는다.

    회귀: 거절을 기록할 경로가 없어 FE 가 카드를 화면에서만 지웠고, 탭을 옮기거나 앱을 다시
    열면 같은 제안이 매번 돌아왔다.
    """
    habit = _habit(freq=5)
    fake_habit_repo.seed(habit)
    _seed_3_weeks(fake_habit_instance_repo, habit, done=1, target=5)
    assert len(client.get("/reviews/habit-penalty").json()["candidates"]) == 1

    resp = _reject(client, f"habit_{habit.id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["habitId"] == f"habit_{habit.id}"
    assert body["frequency"] == 5
    assert "5회" in body["message"]
    assert habit.frequency_per_week == 5  # 빈도는 그대로
    assert habit.last_penalty_decision == "rejected"
    assert client.get("/reviews/habit-penalty").json()["candidates"] == []
    # cooldown 중엔 수락도 안 된다 — 화면에 없는 제안을 적용하지 않는다.
    assert _accept(client, f"habit_{habit.id}", key="late").status_code == 422


def test_reject_cooldown_ends_after_four_weeks(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    from datetime import datetime

    from reaction_backend.schemas.common import KST

    habit = _habit(freq=5)
    fake_habit_repo.seed(habit)
    _seed_3_weeks(fake_habit_instance_repo, habit, done=1, target=5)
    habit.last_penalty_decision = "rejected"
    habit.last_penalty_evaluated_at = datetime.now(KST) - timedelta(days=29)

    candidates = client.get("/reviews/habit-penalty").json()["candidates"]
    assert [c["habitId"] for c in candidates] == [f"habit_{habit.id}"]


def test_reject_twice_does_not_extend_the_cooldown(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    """도메인 멱등 — 두 번 눌러도 기준 시각이 그대로다(Idempotency-Key 없이도 안전)."""
    habit = _habit(freq=5)
    fake_habit_repo.seed(habit)
    _seed_3_weeks(fake_habit_instance_repo, habit, done=1, target=5)

    assert _reject(client, f"habit_{habit.id}").status_code == 200
    first_at = habit.last_penalty_evaluated_at
    again = _reject(client, f"habit_{habit.id}")

    assert again.status_code == 200
    assert habit.last_penalty_evaluated_at == first_at


def test_reject_does_not_overwrite_an_accept_in_the_same_cycle(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    habit = _habit(freq=5)
    fake_habit_repo.seed(habit)
    _seed_3_weeks(fake_habit_instance_repo, habit, done=1, target=5)
    assert _accept(client, f"habit_{habit.id}", key="a").status_code == 200

    resp = _reject(client, f"habit_{habit.id}")

    assert resp.status_code == 422
    assert resp.json()["code"] == "HABIT_PENALTY_NOT_ELIGIBLE"
    assert habit.last_penalty_decision == "accepted"


def test_reject_habit_not_found(client: TestClient) -> None:
    resp = _reject(client, f"habit_{uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "HABIT_NOT_FOUND"


def test_reject_requires_auth(unauthed_client: TestClient) -> None:
    assert unauthed_client.post(f"/reviews/habit-penalty/habit_{uuid4()}/reject").status_code == 401
