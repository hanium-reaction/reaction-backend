"""Weekly Plan + 직접 편집 — #21-B 슬라이스 (api-contract §8 S14/S15).

3층: ① snap/policy 순수 함수 ② GET /plans/weekly ③ PATCH 블록 이동(충돌/정책/스냅).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.time_policy import TimePolicy
from reaction_backend.orchestrator.plan_edit import find_policy_violation, snap_to_15min
from reaction_backend.schemas.common import KST
from tests.conftest import (
    DEMO_USER_UUID,
    FakeActionItemRepo,
    FakeScheduledBlockRepo,
    FakeTimePolicyRepo,
)

_ref = date(2026, 6, 17)
MON = _ref - timedelta(days=_ref.weekday())  # 그 주 월요일


def _dt(day_offset: int, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(MON + timedelta(days=day_offset), time(hour, minute), tzinfo=KST)


def _block(
    start: datetime,
    end: datetime,
    *,
    action_id: object = None,
    status: str = "scheduled",
    source: str = "ai_plan",
) -> ScheduledBlock:
    b = ScheduledBlock()
    b.id = uuid4()
    b.user_id = DEMO_USER_UUID
    b.action_item_id = action_id or uuid4()
    b.start_at = start
    b.end_at = end
    b.block_status = status
    b.source = source
    b.external_calendar_event_id = None
    return b


def _action(category: str = "project") -> ActionItem:
    a = ActionItem()
    a.id = uuid4()
    a.user_id = DEMO_USER_UUID
    a.title = "GROUP BY 실습"
    a.target_date = MON
    a.category = category
    a.source = "goal"
    a.status = "planned"
    a.priority = 3
    a.estimated_minutes = 60
    a.why_now = None
    a.first_step = None
    a.goal_id = None
    a.archived_at = None
    return a


def _policy(ptype: str, payload: dict[str, object], *, active: bool = True) -> TimePolicy:
    p = TimePolicy()
    p.id = uuid4()
    p.user_id = DEMO_USER_UUID
    p.policy_type = ptype
    p.payload = payload
    p.is_active = active
    p.archived_at = None
    return p


# ───────────────────────── snap (순수) ─────────────────────────


def test_snap_rounds_to_nearest_15() -> None:
    assert snap_to_15min(_dt(0, 9, 7)).minute == 0
    assert snap_to_15min(_dt(0, 9, 8)).minute == 15
    assert snap_to_15min(_dt(0, 9, 53)) == _dt(0, 10, 0)


def test_snap_drops_seconds() -> None:
    raw = _dt(0, 9, 0).replace(second=42, microsecond=999)
    assert snap_to_15min(raw).second == 0


# ───────────────────────── policy (순수) ─────────────────────────


def test_policy_sleep_window_wrap() -> None:
    policies = [_policy("sleep", {"start_time": "23:00", "end_time": "07:00"})]
    # 23:30~00:30 → 수면 침범
    assert find_policy_violation(_dt(1, 23, 30), _dt(2, 0, 30), "project", policies) == "sleep"
    # 14:00~15:00 → 안전
    assert find_policy_violation(_dt(1, 14, 0), _dt(1, 15, 0), "project", policies) is None


def test_policy_late_night_category_gated() -> None:
    policies = [
        _policy("late_night_block", {"start_time": "22:00", "blocked_categories": ["study"]})
    ]
    assert (
        find_policy_violation(_dt(1, 22, 30), _dt(1, 23, 0), "study", policies)
        == "late_night_block"
    )
    # 다른 카테고리는 허용
    assert find_policy_violation(_dt(1, 22, 30), _dt(1, 23, 0), "health", policies) is None


def test_policy_inactive_skipped() -> None:
    policies = [_policy("lunch", {"start_time": "12:00", "end_time": "13:00"}, active=False)]
    assert find_policy_violation(_dt(1, 12, 15), _dt(1, 12, 45), "project", policies) is None


# ───────────────────────── GET /plans/weekly ─────────────────────────


def test_get_weekly_groups_by_day(
    client: TestClient, fake_scheduled_block_repo: FakeScheduledBlockRepo
) -> None:
    goal_uuid = uuid4()
    fake_scheduled_block_repo.seed(
        _block(_dt(1, 9, 0), _dt(1, 10, 0)),
        title="화요일 카드",
        category="study",
        goal_id=goal_uuid,
    )
    fake_scheduled_block_repo.seed(
        _block(_dt(2, 14, 0), _dt(2, 15, 0)), title="수요일 카드", category="project"
    )
    resp = client.get("/plans/weekly", params={"weekStart": MON.isoformat()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["planId"] == f"plan_{MON.isoformat()}"
    assert len(body["days"]) == 7
    tue = body["days"][1]
    assert tue["weekday"] == "tuesday"
    assert len(tue["blocks"]) == 1
    assert tue["blocks"][0]["blockId"].startswith("block_")
    # 블록 → 목표 연결: action_item.goal_id 가 goal_<uuid> 로 내려온다 (FE 분류/색 연결용).
    assert tue["blocks"][0]["goalId"] == f"goal_{goal_uuid}"
    assert body["days"][2]["blocks"][0]["title"] == "수요일 카드"
    # 목표 미연결 액션(inbox 등)은 null.
    assert body["days"][2]["blocks"][0]["goalId"] is None


def test_get_weekly_empty(client: TestClient) -> None:
    resp = client.get("/plans/weekly", params={"weekStart": MON.isoformat()})
    assert resp.status_code == 200
    assert all(len(d["blocks"]) == 0 for d in resp.json()["days"])


def test_get_weekly_excludes_cancelled(
    client: TestClient, fake_scheduled_block_repo: FakeScheduledBlockRepo
) -> None:
    """계획 교체(승인) 등으로 cancelled 된 블록은 주간 그리드에서 제외."""
    fake_scheduled_block_repo.seed(
        _block(_dt(1, 9, 0), _dt(1, 10, 0), status="cancelled"),
        title="취소된 카드",
        category="study",
    )
    fake_scheduled_block_repo.seed(
        _block(_dt(1, 11, 0), _dt(1, 12, 0)), title="유효 카드", category="study"
    )
    resp = client.get("/plans/weekly", params={"weekStart": MON.isoformat()})
    assert resp.status_code == 200
    tue = resp.json()["days"][1]
    assert [b["title"] for b in tue["blocks"]] == ["유효 카드"]


def test_get_weekly_invalid(client: TestClient) -> None:
    resp = client.get("/plans/weekly", params={"weekStart": "2026/06/15"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "PLAN_INVALID_TIME"


def test_weekly_requires_auth(unauthed_client: TestClient) -> None:
    assert unauthed_client.get("/plans/weekly").status_code == 401


# ───────────────────────── PATCH 블록 이동 ─────────────────────────


def _patch(client: TestClient, block_id: str, body: dict[str, object]) -> object:
    return client.patch(f"/plans/plan_{MON.isoformat()}/blocks/{block_id}", json=body)


def test_edit_block_moves_and_snaps(
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    action = _action()
    action.goal_id = uuid4()
    fake_action_item_repo.seed(action)
    block = _block(_dt(1, 9, 0), _dt(1, 10, 0), action_id=action.id)
    fake_scheduled_block_repo.seed(
        block, title=action.title, category=action.category, goal_id=action.goal_id
    )

    resp = _patch(client, f"block_{block.id}", {"startAt": _dt(1, 11, 7).isoformat()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "user_edit"
    # 11:07 → 11:00 스냅, 길이(1h) 보존 → 12:00
    assert body["startAt"].startswith(f"{(MON + timedelta(days=1)).isoformat()}T11:00")
    assert body["endAt"].startswith(f"{(MON + timedelta(days=1)).isoformat()}T12:00")
    # 이동 응답도 목표 연결을 에코 — null 이면 FE 그리드에서 블록이 '기타' 로 되돌아간다.
    assert body["goalId"] == f"goal_{action.goal_id}"


def test_edit_block_conflict(
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    action = _action()
    fake_action_item_repo.seed(action)
    moving = _block(_dt(1, 9, 0), _dt(1, 10, 0), action_id=action.id)
    fake_scheduled_block_repo.seed(moving, title=action.title, category=action.category)
    # 충돌 대상 — 14:00~15:00
    other = _block(_dt(1, 14, 0), _dt(1, 15, 0))
    fake_scheduled_block_repo.seed(other, title="기존", category="project")

    resp = _patch(client, f"block_{moving.id}", {"startAt": _dt(1, 14, 0).isoformat()})
    assert resp.status_code == 422
    assert resp.json()["code"] == "PLAN_BLOCK_CONFLICT"


def test_edit_block_policy_violation(
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
    fake_time_policy_repo: FakeTimePolicyRepo,
) -> None:
    action = _action()
    fake_action_item_repo.seed(action)
    block = _block(_dt(1, 9, 0), _dt(1, 10, 0), action_id=action.id)
    fake_scheduled_block_repo.seed(block, title=action.title, category=action.category)
    policy = _policy("sleep", {"start_time": "23:00", "end_time": "07:00"})
    fake_time_policy_repo._items[policy.id] = policy

    resp = _patch(client, f"block_{block.id}", {"startAt": _dt(1, 23, 30).isoformat()})
    assert resp.status_code == 422
    assert resp.json()["code"] == "POLICY_VIOLATION"


def test_edit_block_invalid_time(
    client: TestClient,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    action = _action()
    fake_action_item_repo.seed(action)
    block = _block(_dt(1, 9, 0), _dt(1, 10, 0), action_id=action.id)
    fake_scheduled_block_repo.seed(block, title=action.title, category=action.category)

    resp = _patch(
        client,
        f"block_{block.id}",
        {"startAt": _dt(1, 10, 0).isoformat(), "endAt": _dt(1, 9, 0).isoformat()},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "PLAN_INVALID_TIME"


def test_edit_block_not_found(client: TestClient) -> None:
    resp = _patch(client, f"block_{uuid4()}", {"startAt": _dt(1, 11, 0).isoformat()})
    assert resp.status_code == 404
    assert resp.json()["code"] == "PLAN_BLOCK_NOT_FOUND"


def test_edit_block_bad_id(client: TestClient) -> None:
    resp = _patch(client, "not-a-block", {"startAt": _dt(1, 11, 0).isoformat()})
    assert resp.status_code == 404
    assert resp.json()["code"] == "PLAN_BLOCK_NOT_FOUND"
