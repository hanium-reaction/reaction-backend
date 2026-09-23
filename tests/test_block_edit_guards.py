"""주간 캘린더 블록 편집(PATCH /plans/{planId}/blocks/{blockId})의 가드.

- planA-9: 이미 시작했거나 끝낸 블록은 시간을 옮길 수 없다(제목·목표만 바꾸는 건 허용).
- planA-4: 300자를 넘는 제목은 한국어 422 로 거절한다(예전엔 UPDATE 에서 터져 일반 500).
- planA-13: 고정 일정(수업)·노터치 시간 위로는 못 옮긴다. 주간 그리드에 고정 일정이 함께 온다.
  시각을 그대로 보낸 제목·목표 편집은 겹침·정책 검사도 15분 snap 도 하지 않는다(리뷰 반영).
- calendar-P1: 승인·재계획과 **같은 정책 집합**을 본다 — DB `time_policies` 가 없어도 인터뷰
  활동 시간대 밖(수면)으로는 못 옮긴다. 예전엔 DB 정책만 봐서 사실상 아무것도 막지 않았다.
"""

from __future__ import annotations

from datetime import time
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from reaction_backend.db.models.fixed_schedule import FixedSchedule
from tests.conftest import (
    DEMO_USER_UUID,
    FakeActionItemRepo,
    FakeFixedScheduleRepo,
    FakeScheduledBlockRepo,
    FakeTimePolicyRepo,
)
from tests.test_planning_weekly import MON, _action, _block, _dt, _patch, _policy
from tests.test_replan_activity_window import _outcome_with_window, _use_outcome


@pytest.mark.parametrize("status", ["started", "finished"])
def test_a_started_or_finished_block_cannot_be_moved(
    status: str,
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """끝낸 블록을 다음 주로 끌면 수행 기록이 미래로 가고 카드가 오늘 화면에서 사라졌다."""
    action = _action()
    fake_action_item_repo.seed(action)
    block = _block(_dt(1, 9, 0), _dt(1, 10, 0), action_id=action.id, status=status)
    fake_scheduled_block_repo.seed(block, title=action.title, category=action.category)

    resp: Any = _patch(client, f"block_{block.id}", {"startAt": _dt(4, 15, 0).isoformat()})

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "PLAN_INVALID_TIME"
    assert "옮길 수 없어요" in body["message"]
    assert (block.start_at, block.end_at, block.source) == (_dt(1, 9, 0), _dt(1, 10, 0), "ai_plan")
    assert action.target_date == MON


def test_a_finished_block_can_still_be_renamed(
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """시각을 그대로 보내고 제목만 바꾸면 통과 — 시각·출처는 건드리지 않는다."""
    action = _action()
    fake_action_item_repo.seed(action)
    block = _block(_dt(1, 9, 0), _dt(1, 10, 0), action_id=action.id, status="finished")
    fake_scheduled_block_repo.seed(block, title=action.title, category=action.category)
    # 끝낸 블록과 겹치는 다른 블록이 있어도 제목 편집은 막지 않는다(시간을 안 옮기니까).
    fake_scheduled_block_repo.seed(
        _block(_dt(1, 9, 30), _dt(1, 10, 30)), title="겹친 일정", category="study"
    )

    resp: Any = _patch(
        client,
        f"block_{block.id}",
        {"startAt": _dt(1, 9, 0).isoformat(), "title": "SQL 복습 끝"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "SQL 복습 끝"
    assert body["blockStatus"] == "finished"
    assert body["source"] == "ai_plan"
    assert body["startAt"].startswith(f"{_dt(1, 9, 0).date().isoformat()}T09:00")


def test_a_title_longer_than_the_card_column_is_rejected_in_korean(
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    action = _action()
    fake_action_item_repo.seed(action)
    block = _block(_dt(1, 9, 0), _dt(1, 10, 0), action_id=action.id)
    fake_scheduled_block_repo.seed(block, title=action.title, category=action.category)

    too_long: Any = _patch(
        client, f"block_{block.id}", {"startAt": _dt(1, 9, 0).isoformat(), "title": "가" * 301}
    )
    assert too_long.status_code == 422
    assert too_long.json()["code"] == "COMMON_VALIDATION_ERROR"
    assert too_long.json()["message"] == "제목은 300자까지 쓸 수 있어요. 조금 줄여 주세요."
    assert action.title == "GROUP BY 실습"

    exact: Any = _patch(
        client, f"block_{block.id}", {"startAt": _dt(1, 9, 0).isoformat(), "title": "가" * 300}
    )
    assert exact.status_code == 200, exact.text
    assert action.title == "가" * 300


def _class_on_monday(repo: FakeFixedScheduleRepo) -> FixedSchedule:
    s = FixedSchedule()
    s.id = uuid4()
    s.user_id = DEMO_USER_UUID
    s.title = "자료구조 수업"
    s.days_of_week = ["mon"]
    s.start_time = time(10, 0)
    s.end_time = time(12, 0)
    s.archived_at = None
    repo._items[s.id] = s
    return s


def _movable(blocks: FakeScheduledBlockRepo, actions: FakeActionItemRepo) -> tuple[Any, Any]:
    action = _action()
    actions.seed(action)
    block = _block(_dt(1, 9, 0), _dt(1, 10, 0), action_id=action.id)  # 화요일 09:00
    blocks.seed(block, title=action.title, category=action.category)
    return action, block


def test_a_block_cannot_be_dropped_onto_a_class(
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
    fake_fixed_schedule_repo: FakeFixedScheduleRepo,
) -> None:
    _class_on_monday(fake_fixed_schedule_repo)
    _, block = _movable(fake_scheduled_block_repo, fake_action_item_repo)

    onto_class: Any = _patch(client, f"block_{block.id}", {"startAt": _dt(0, 10, 30).isoformat()})
    assert onto_class.status_code == 422
    assert onto_class.json()["code"] == "PLAN_BLOCK_CONFLICT"
    assert "자료구조 수업" in onto_class.json()["message"]
    assert block.start_at == _dt(1, 9, 0) and block.source == "ai_plan"

    right_after: Any = _patch(client, f"block_{block.id}", {"startAt": _dt(0, 12, 0).isoformat()})
    assert right_after.status_code == 200, right_after.text


def test_a_block_already_on_a_class_can_still_be_renamed_where_it_is(
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
    fake_fixed_schedule_repo: FakeFixedScheduleRepo,
) -> None:
    """리뷰 반영: 수업을 나중에 추가해 이미 수업 위에 놓인 블록도, 시각을 그대로 보내고 제목만
    바꾸면 200 — 겹침 검사는 시간을 옮기는 편집에만 건다(예전엔 이름조차 못 바꿨다)."""
    _class_on_monday(fake_fixed_schedule_repo)
    action = _action()
    fake_action_item_repo.seed(action)
    block = _block(_dt(0, 10, 30), _dt(0, 11, 30), action_id=action.id)  # 월 수업 10~12 위
    fake_scheduled_block_repo.seed(block, title=action.title, category=action.category)

    resp: Any = _patch(
        client,
        f"block_{block.id}",
        {
            "startAt": _dt(0, 10, 30).isoformat(),
            "endAt": _dt(0, 11, 30).isoformat(),
            "title": "SQL 복습",
        },
    )

    assert resp.status_code == 200, resp.text
    assert action.title == "SQL 복습"
    assert (block.start_at, block.end_at) == (_dt(0, 10, 30), _dt(0, 11, 30))


def test_renaming_an_off_grid_block_does_not_shift_it(
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """스케줄러 블록은 15분 격자가 아니다(수업 끝 10:50 시작). 제목만 바꾸는 편집이 snap 으로
    10:45 로 당겨지지 않는다."""
    action = _action()
    fake_action_item_repo.seed(action)
    block = _block(_dt(1, 10, 50), _dt(1, 11, 50), action_id=action.id)
    fake_scheduled_block_repo.seed(block, title=action.title, category=action.category)

    resp: Any = _patch(
        client, f"block_{block.id}", {"startAt": _dt(1, 10, 50).isoformat(), "title": "SQL 복습"}
    )

    assert resp.status_code == 200, resp.text
    assert (block.start_at, block.end_at) == (_dt(1, 10, 50), _dt(1, 11, 50))
    assert resp.json()["startAt"].startswith(f"{_dt(1, 0).date().isoformat()}T10:50")


def test_a_class_on_another_weekday_does_not_block(
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
    fake_fixed_schedule_repo: FakeFixedScheduleRepo,
) -> None:
    _class_on_monday(fake_fixed_schedule_repo)
    _, block = _movable(fake_scheduled_block_repo, fake_action_item_repo)

    resp: Any = _patch(client, f"block_{block.id}", {"startAt": _dt(2, 10, 30).isoformat()})
    assert resp.status_code == 200, resp.text


def test_a_block_cannot_be_dropped_into_a_no_touch_window(
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
    fake_time_policy_repo: FakeTimePolicyRepo,
) -> None:
    policy = _policy(
        "no_touch", {"start_time": "18:00", "end_time": "20:00", "days_of_week": ["sun"]}
    )
    fake_time_policy_repo._items[policy.id] = policy
    _, block = _movable(fake_scheduled_block_repo, fake_action_item_repo)

    resp: Any = _patch(client, f"block_{block.id}", {"startAt": _dt(6, 18, 30).isoformat()})
    assert resp.status_code == 422
    assert resp.json()["code"] == "POLICY_VIOLATION"
    assert "no_touch" not in resp.json()["message"]


def test_policy_messages_do_not_show_internal_codes(
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
    fake_time_policy_repo: FakeTimePolicyRepo,
) -> None:
    policy = _policy("sleep", {"start_time": "23:00", "end_time": "07:00"})
    fake_time_policy_repo._items[policy.id] = policy
    _, block = _movable(fake_scheduled_block_repo, fake_action_item_repo)

    resp: Any = _patch(client, f"block_{block.id}", {"startAt": _dt(1, 23, 30).isoformat()})
    assert resp.status_code == 422
    assert "sleep" not in resp.json()["message"]
    assert "수면" in resp.json()["message"]


def test_weekly_grid_shows_fixed_schedules_on_their_day(
    client: TestClient, fake_fixed_schedule_repo: FakeFixedScheduleRepo
) -> None:
    _class_on_monday(fake_fixed_schedule_repo)

    resp = client.get("/plans/weekly", params={"weekStart": MON.isoformat()})

    assert resp.status_code == 200
    days = resp.json()["days"]
    assert [f["title"] for f in days[0]["fixedSchedules"]] == ["자료구조 수업"]
    assert days[0]["fixedSchedules"][0]["startAt"].startswith(f"{MON.isoformat()}T10:00")
    assert days[0]["fixedSchedules"][0]["endAt"].startswith(f"{MON.isoformat()}T12:00")
    assert all(d["fixedSchedules"] == [] for d in days[1:])


# ── 인터뷰 활동 시간대 (calendar-P1) ──────────────────────────────────────────
#
# 편집기는 DB `time_policies` 만 봤는데 그 행을 만드는 FE 화면이 없어 실사용자는 늘 빈
# 목록이었다 — 08~16 시에만 활동한다고 답한 학생이 블록을 새벽 3시로 끌어도 200 이었고,
# 같은 학생이 승인·재계획에서는 02:00 이 막혔다. 이제 편집도 같은 정책 집합을 본다.


def _interviewed(monkeypatch: Any, repo: Any, start: str, end: str) -> None:
    """활동 시간대만 다른 '정상 종료' 인터뷰를 그 사용자에게 붙인다 (DB 정책 행은 없다)."""
    _use_outcome(monkeypatch, repo, _outcome_with_window(start, end))


@pytest.mark.parametrize(
    ("day", "hour"),
    [
        (1, 3),  # 활동 시작(08:00) 전 새벽 — 실측 리포트의 그 시각
        (1, 22),  # 활동 종료(16:00) 뒤 밤 — 여집합의 '16:00~24:00' 조각
    ],
)
def test_a_move_outside_the_interview_activity_window_is_rejected_without_any_policy_row(
    day: int,
    hour: int,
    monkeypatch: Any,
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
    fake_interview_repo: Any,
) -> None:
    _interviewed(monkeypatch, fake_interview_repo, "08:00", "16:00")
    _, block = _movable(fake_scheduled_block_repo, fake_action_item_repo)

    resp: Any = _patch(client, f"block_{block.id}", {"startAt": _dt(day, hour, 0).isoformat()})

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "POLICY_VIOLATION"
    assert "수면" in body["message"]
    assert body["field"] == "startAt"
    # 422 면 블록은 원래 자리에 그대로다 (커밋 전에 막는다).
    assert (block.start_at, block.source) == (_dt(1, 9, 0), "ai_plan")


def test_a_move_inside_the_interview_activity_window_still_succeeds(
    monkeypatch: Any,
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
    fake_interview_repo: Any,
) -> None:
    """같은 사용자가 활동 시간대 안(13:00)으로 옮기는 건 종전 그대로 200."""
    _interviewed(monkeypatch, fake_interview_repo, "08:00", "16:00")
    _, block = _movable(fake_scheduled_block_repo, fake_action_item_repo)

    resp: Any = _patch(client, f"block_{block.id}", {"startAt": _dt(1, 13, 0).isoformat()})

    assert resp.status_code == 200, resp.text
    assert block.start_at == _dt(1, 13, 0)
    assert block.source == "user_edit"


def test_a_db_policy_row_still_decides_on_its_own(
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
    fake_time_policy_repo: FakeTimePolicyRepo,
) -> None:
    """인터뷰가 없고 DB 정책만 있는 사용자 — 예전과 같은 판정·같은 문구.

    저장된 점심(12~13시) 정책은 여전히 422 `POLICY_VIOLATION` 이고, 그 밖의 시간은 통과한다
    (활동 시간대를 모르는 사용자에게 없던 창을 새로 씌우지 않는다).
    """
    policy = _policy("lunch", {"start_time": "12:00", "end_time": "13:00"})
    fake_time_policy_repo._items[policy.id] = policy
    _, block = _movable(fake_scheduled_block_repo, fake_action_item_repo)

    onto_lunch: Any = _patch(client, f"block_{block.id}", {"startAt": _dt(1, 12, 30).isoformat()})
    assert onto_lunch.status_code == 422
    assert onto_lunch.json()["code"] == "POLICY_VIOLATION"
    assert "점심" in onto_lunch.json()["message"]
    assert "lunch" not in onto_lunch.json()["message"]

    after_lunch: Any = _patch(client, f"block_{block.id}", {"startAt": _dt(1, 13, 0).isoformat()})
    assert after_lunch.status_code == 200, after_lunch.text
