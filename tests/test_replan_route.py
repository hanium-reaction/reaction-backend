"""주간 forward 재계획 라우트 (#117 재작업) — 생성 + **block-id 재조정** 승인 검증.

핵심(#117 fix): 승인이 blanket-cancel 대신, payload 의 각 블록마다 '교체할 옛 블록'을
현재 DB 상태로 재조정한다.
- 옛 블록이 여전히 `scheduled` → 그 블록만 취소 + 새 블록 생성.
- 그새 `started`/`cancelled`(다른 계획이 취소) → 취소·생성 모두 skip(손실·중복 방지).
- payload 에 없는 블록(드롭된 후보의 옛 블록)은 손대지 않아 보존.
- 백로그(옛 블록 없음)인데 그새 활성 블록이 생기면 생성 skip.

또한 생성 단계에서 busy(확정 블록 + 고정일정 #112 정합)를 회피하는지 확인한다.
ADR-0005 §7.3 패턴: LLM 미호출(룰 스케줄러) — HTTP 레벨 실배선.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.fixed_schedule import FixedSchedule
from reaction_backend.db.models.plan_draft import PlanDraft
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.schemas.common import KST
from tests.conftest import (
    DEMO_USER_UUID,
    FakeActionItemRepo,
    FakeFixedScheduleRepo,
    FakePlanDraftRepo,
    FakeScheduledBlockRepo,
)

# 고정 기준일: 2026-07-09(목). next_week_start → 2026-07-13(월)이 재배치 창 시작.
FROZEN_NOW = datetime(2026, 7, 9, 12, 0, tzinfo=KST)
WINDOW_START = date(2026, 7, 13)
_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _freeze_now(monkeypatch: Any, dt: datetime = FROZEN_NOW) -> None:
    """라우트가 참조하는 now_kst 를 고정 — 재배치 창을 달력과 무관하게 결정적으로."""
    import reaction_backend.api.routes.planning as planning_mod

    monkeypatch.setattr(planning_mod, "now_kst", lambda: dt)


def _kst(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=KST)


def _seed_action(
    repo: FakeActionItemRepo,
    *,
    title: str,
    category: str = "study",
    est: int = 30,
    status: str = "planned",
    target: date | None = None,
    archived: bool = False,
) -> ActionItem:
    a = ActionItem()
    a.id = uuid4()
    a.user_id = DEMO_USER_UUID
    a.title = title
    a.category = category
    a.status = status
    a.priority = 3
    a.estimated_minutes = est
    a.target_date = target
    a.source = "recovery_downscope"
    a.parent_action_item_id = None
    a.inbox_item_id = None
    a.why_now = None
    a.first_step = None
    a.goal_id = None
    a.archived_at = datetime.now(UTC) if archived else None
    repo.seed(a)
    return a


def _seed_block(
    repo: FakeScheduledBlockRepo,
    *,
    action_id: UUID,
    start: datetime,
    end: datetime,
    status: str = "scheduled",
    source: str = "ai_plan",
    title: str = "블록",
    category: str = "study",
) -> ScheduledBlock:
    b = ScheduledBlock()
    b.id = uuid4()
    b.user_id = DEMO_USER_UUID
    b.action_item_id = action_id
    b.start_at = start
    b.end_at = end
    b.block_status = status
    b.source = source
    b.external_calendar_event_id = None
    repo.seed(b, title=title, category=category)
    return b


def _seed_fixed(
    repo: FakeFixedScheduleRepo,
    *,
    start: time,
    end: time,
    title: str = "수업",
) -> FixedSchedule:
    s = FixedSchedule()
    s.id = uuid4()
    s.user_id = DEMO_USER_UUID
    s.title = title
    s.days_of_week = list(_WEEKDAY_KEYS)  # 매일
    s.start_time = start
    s.end_time = end
    s.archived_at = None
    repo._items[s.id] = s
    return s


def _seed_replan_draft(
    repo: FakePlanDraftRepo,
    *,
    blocks: list[dict[str, Any]],
    status: str = "draft",
) -> str:
    d = PlanDraft()
    d.id = uuid4()
    d.user_id = DEMO_USER_UUID
    d.status = status
    d.target_date = WINDOW_START
    d.horizon = "2026-07-17"
    d.ai_source = "rule"
    d.payload = {
        "kind": "replan",
        "window_start": WINDOW_START.isoformat(),
        "horizon": "2026-07-17",
        "blocks": blocks,
        "warnings": [],
    }
    d.expires_at = FROZEN_NOW + timedelta(days=1)
    d.approved_at = None
    d.created_at = datetime.now(UTC)
    d.updated_at = datetime.now(UTC)
    repo._items[d.id] = d
    return str(d.id)


def _pblock(
    *,
    action_id: UUID,
    start: datetime,
    end: datetime,
    replaces: UUID | None = None,
) -> dict[str, Any]:
    return {
        "actionId": f"action_{action_id}",
        "title": "재배치",
        "category": "study",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "replacesBlockId": f"block_{replaces}" if replaces is not None else None,
    }


def _overlaps(b: ScheduledBlock, start: datetime, end: datetime) -> bool:
    return b.start_at < end and b.end_at > start


# ── 생성(generate) ───────────────────────────────────────────────────────────


def test_generate_wires_replaces_id_and_backlog(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> None:
    """미래 미착수 블록의 액션 → replacesBlockId 실림. 활성 블록 없는 planned → 백로그(None)."""
    _freeze_now(monkeypatch)
    action_a = _seed_action(fake_action_item_repo, title="A")
    old = _seed_block(
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 15, 10, 0),
        end=_kst(2026, 7, 15, 10, 30),
    )
    action_b = _seed_action(fake_action_item_repo, title="B")  # 블록 없음 → 백로그

    resp = client.post("/plans/replan")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    by_action = {b["actionId"]: b for b in body["blocks"]}
    assert f"action_{action_a.id}" in by_action
    assert f"action_{action_b.id}" in by_action
    # A: 교체할 옛 블록 id 가 실린다. B: 백로그라 None.
    assert by_action[f"action_{action_a.id}"]["replacesBlockId"] == f"block_{old.id}"
    assert by_action[f"action_{action_b.id}"]["replacesBlockId"] is None


def test_generate_backlog_only_spreads_over_week_not_one_day(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """마감 신호 없는 백로그만 있을 때, 창이 하루로 붕괴하지 않고 한 주에 분산된다(#117 fix#4).

    미래 블록 0 + target_date 과거/None → deadline 이 window_start 로 축소되면 다음 주
    월요일 하루에 몰린다. 최소 한 주 지평 가드가 있어야 여러 날에 흩어진다.
    """
    _freeze_now(monkeypatch)
    # 6개 × 45분 = 270분 > 하루 집중 상한(180분): 하루면 일부가 warnings 로 드롭된다.
    for i in range(6):
        _seed_action(
            fake_action_item_repo,
            title=f"백로그{i}",
            est=45,
            target=date(2026, 7, 1),  # 과거 — deadline 을 밀지 못함
        )

    resp = client.post("/plans/replan")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # window_start(07-13, 월)~그 주 일요일(07-19) 안에서 배치되고, 하루에 다 몰리지 않는다.
    assert body["horizon"] == "2026-07-19"
    days = {datetime.fromisoformat(b["start"]).date() for b in body["blocks"]}
    assert len(body["blocks"]) == 6  # 전량 배치(드롭 없음)
    assert len(days) >= 3  # 최소 3일에 분산


def test_generate_avoids_committed_and_fixed_busy(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_fixed_schedule_repo: FakeFixedScheduleRepo,
) -> None:
    """확정(started) 블록 + 고정일정(#112 정합)을 busy 로 피해 배치한다."""
    _freeze_now(monkeypatch)
    _seed_action(fake_action_item_repo, title="백로그", target=date(2026, 7, 16))
    # 확정(started) 블록 — 07-14 09:00~12:00 은 회피 대상.
    _seed_block(
        fake_scheduled_block_repo,
        action_id=uuid4(),
        start=_kst(2026, 7, 14, 9, 0),
        end=_kst(2026, 7, 14, 12, 0),
        status="started",
    )
    # 고정일정 매일 13:00~18:00 — 절대 침범 불가.
    _seed_fixed(fake_fixed_schedule_repo, start=time(13, 0), end=time(18, 0))

    resp = client.post("/plans/replan")
    assert resp.status_code == 201, resp.text
    blocks = resp.json()["blocks"]
    assert blocks  # 적어도 하나는 배치
    for b in blocks:
        start = datetime.fromisoformat(b["start"])
        end = datetime.fromisoformat(b["end"])
        # 확정 블록 구간(07-14 09~12)과 겹치지 않는다.
        assert not (start < _kst(2026, 7, 14, 12, 0) and end > _kst(2026, 7, 14, 9, 0))
        # 고정일정 구간(13~18, 매일)과 겹치지 않는다.
        assert not (start.time() < time(18, 0) and end.time() > time(13, 0))


# ── 승인 재조정(approve reconcile) ───────────────────────────────────────────


def test_approve_replaces_still_scheduled_block(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """옛 블록이 여전히 scheduled → 그 블록만 취소 + 새 블록 생성."""
    _freeze_now(monkeypatch)
    action_a = _seed_action(fake_action_item_repo, title="A")
    old = _seed_block(
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 15, 10, 0),
        end=_kst(2026, 7, 15, 10, 30),
    )
    draft_id = _seed_replan_draft(
        fake_plan_draft_repo,
        blocks=[
            _pblock(
                action_id=action_a.id,
                start=_kst(2026, 7, 14, 8, 0),
                end=_kst(2026, 7, 14, 8, 30),
                replaces=old.id,
            )
        ],
    )

    resp = client.post(f"/plans/replan/{draft_id}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["cancelledBlocks"], body["createdBlocks"], body["skippedBlocks"]) == (1, 1, 0)
    assert body["isDraft"] is False
    # 옛 블록은 취소, 새 블록(ai_plan, 07-14 08:00)이 생겼다.
    assert fake_scheduled_block_repo._blocks[old.id].block_status == "cancelled"
    new_blocks = [
        b
        for b in fake_scheduled_block_repo._blocks.values()
        if b.action_item_id == action_a.id and b.block_status == "scheduled"
    ]
    assert len(new_blocks) == 1
    assert new_blocks[0].start_at == _kst(2026, 7, 14, 8, 0)


def test_approve_skips_when_old_block_started(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """그새 started 로 바뀐 옛 블록 → 취소·생성 모두 skip(손실 방지)."""
    _freeze_now(monkeypatch)
    action_a = _seed_action(fake_action_item_repo, title="A")
    old = _seed_block(
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 15, 10, 0),
        end=_kst(2026, 7, 15, 10, 30),
        status="started",
    )
    draft_id = _seed_replan_draft(
        fake_plan_draft_repo,
        blocks=[
            _pblock(
                action_id=action_a.id,
                start=_kst(2026, 7, 14, 8, 0),
                end=_kst(2026, 7, 14, 8, 30),
                replaces=old.id,
            )
        ],
    )

    resp = client.post(f"/plans/replan/{draft_id}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["cancelledBlocks"], body["createdBlocks"], body["skippedBlocks"]) == (0, 0, 1)
    # 옛 블록은 그대로 started, 새 블록 없음.
    assert fake_scheduled_block_repo._blocks[old.id].block_status == "started"
    assert not [
        b
        for b in fake_scheduled_block_repo._blocks.values()
        if b.action_item_id == action_a.id and b.block_status == "scheduled"
    ]


def test_approve_skips_when_old_block_cancelled_concurrently(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """다른 계획이 그새 취소한 옛 블록 → skip(중복 생성 방지)."""
    _freeze_now(monkeypatch)
    action_a = _seed_action(fake_action_item_repo, title="A")
    old = _seed_block(
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 15, 10, 0),
        end=_kst(2026, 7, 15, 10, 30),
        status="cancelled",
    )
    draft_id = _seed_replan_draft(
        fake_plan_draft_repo,
        blocks=[
            _pblock(
                action_id=action_a.id,
                start=_kst(2026, 7, 14, 8, 0),
                end=_kst(2026, 7, 14, 8, 30),
                replaces=old.id,
            )
        ],
    )

    resp = client.post(f"/plans/replan/{draft_id}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["cancelledBlocks"], body["createdBlocks"], body["skippedBlocks"]) == (0, 0, 1)


def test_approve_preserves_unreferenced_block(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """payload 에 없는 옛 블록(드롭된 후보)은 손대지 않는다 — 백로그 항목만 생성."""
    _freeze_now(monkeypatch)
    action_x = _seed_action(fake_action_item_repo, title="X")
    untouched = _seed_block(
        fake_scheduled_block_repo,
        action_id=action_x.id,
        start=_kst(2026, 7, 15, 10, 0),
        end=_kst(2026, 7, 15, 10, 30),
    )
    action_b = _seed_action(fake_action_item_repo, title="백로그")
    draft_id = _seed_replan_draft(
        fake_plan_draft_repo,
        blocks=[
            _pblock(
                action_id=action_b.id,
                start=_kst(2026, 7, 14, 8, 0),
                end=_kst(2026, 7, 14, 8, 30),
                replaces=None,
            )
        ],
    )

    resp = client.post(f"/plans/replan/{draft_id}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["cancelledBlocks"], body["createdBlocks"], body["skippedBlocks"]) == (0, 1, 0)
    # 참조되지 않은 블록은 그대로 scheduled.
    assert fake_scheduled_block_repo._blocks[untouched.id].block_status == "scheduled"


def test_approve_skips_backlog_when_active_block_appeared(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """백로그(옛 블록 없음)인데 그새 활성 블록이 생겼으면 생성 skip(중복 방지)."""
    _freeze_now(monkeypatch)
    action_b = _seed_action(fake_action_item_repo, title="백로그")
    # 그새 다른 경로로 활성 블록이 생김.
    _seed_block(
        fake_scheduled_block_repo,
        action_id=action_b.id,
        start=_kst(2026, 7, 15, 9, 0),
        end=_kst(2026, 7, 15, 9, 30),
    )
    draft_id = _seed_replan_draft(
        fake_plan_draft_repo,
        blocks=[
            _pblock(
                action_id=action_b.id,
                start=_kst(2026, 7, 14, 8, 0),
                end=_kst(2026, 7, 14, 8, 30),
                replaces=None,
            )
        ],
    )

    resp = client.post(f"/plans/replan/{draft_id}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["cancelledBlocks"], body["createdBlocks"], body["skippedBlocks"]) == (0, 0, 1)


def test_approve_skips_when_action_archived_meanwhile(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """generate~approve 사이 action 이 아카이브되면(예: #113 supersede) 항목 전체 skip."""
    _freeze_now(monkeypatch)
    action_a = _seed_action(fake_action_item_repo, title="A", archived=True)
    draft_id = _seed_replan_draft(
        fake_plan_draft_repo,
        blocks=[
            _pblock(
                action_id=action_a.id,
                start=_kst(2026, 7, 14, 8, 0),
                end=_kst(2026, 7, 14, 8, 30),
                replaces=None,
            )
        ],
    )

    resp = client.post(f"/plans/replan/{draft_id}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["cancelledBlocks"], body["createdBlocks"], body["skippedBlocks"]) == (0, 0, 1)
    # 아카이브된 카드에 좀비 블록이 생기지 않는다.
    assert not [
        b for b in fake_scheduled_block_repo._blocks.values() if b.action_item_id == action_a.id
    ]


def test_first_plan_approve_rejects_replan_draft(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """재계획 Draft 를 First Plan 승인(`/plans/{id}/approve`)에 넣으면 500 대신 404 로 안내."""
    _freeze_now(monkeypatch)
    action_a = _seed_action(fake_action_item_repo, title="A")
    draft_id = _seed_replan_draft(
        fake_plan_draft_repo,
        blocks=[
            _pblock(
                action_id=action_a.id,
                start=_kst(2026, 7, 14, 8, 0),
                end=_kst(2026, 7, 14, 8, 30),
                replaces=None,
            )
        ],
    )

    resp = client.post(f"/plans/{draft_id}/approve")
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "PLAN_DRAFT_NOT_FOUND"


def test_approve_idempotent_when_already_approved(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """이미 승인된 Draft 재승인 → 재조정 없이 created=len, cancelled=0(신규 블록 안 생김)."""
    _freeze_now(monkeypatch)
    action_a = _seed_action(fake_action_item_repo, title="A")
    draft_id = _seed_replan_draft(
        fake_plan_draft_repo,
        status="approved",
        blocks=[
            _pblock(
                action_id=action_a.id,
                start=_kst(2026, 7, 14, 8, 0),
                end=_kst(2026, 7, 14, 8, 30),
                replaces=None,
            )
        ],
    )
    before = len(fake_scheduled_block_repo._blocks)

    resp = client.post(f"/plans/replan/{draft_id}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["cancelledBlocks"], body["createdBlocks"], body["skippedBlocks"]) == (0, 1, 0)
    # 멱등 — 실제 블록은 새로 생기지 않는다.
    assert len(fake_scheduled_block_repo._blocks) == before
