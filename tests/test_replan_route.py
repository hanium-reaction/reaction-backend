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
from reaction_backend.schemas.common import KST, now_kst
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)
from tests.conftest import (
    DEMO_USER_UUID,
    FakeActionItemRepo,
    FakeFixedScheduleRepo,
    FakeInterviewRepo,
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
    old_blocks: dict[str, list[str]] | None = None,
) -> str:
    d = PlanDraft()
    d.id = uuid4()
    d.user_id = DEMO_USER_UUID
    d.status = status
    d.target_date = WINDOW_START
    d.horizon = "2026-07-17"
    d.ai_source = "rule"
    # oldBlocks(재조정 권위 맵): 미지정이면 블록의 replacesBlockId 에서 액션당 파생.
    if old_blocks is None:
        old_blocks = {}
        for b in blocks:
            rid = b.get("replacesBlockId")
            if rid:
                lst = old_blocks.setdefault(b["actionId"], [])
                if rid not in lst:
                    lst.append(rid)
    d.payload = {
        "kind": "replan",
        "window_start": WINDOW_START.isoformat(),
        "horizon": "2026-07-17",
        "blocks": blocks,
        "oldBlocks": old_blocks,
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


def test_generate_picks_up_stale_never_started_card(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> None:
    """**핵심 회귀 테스트.** 시작 시각이 지났는데 한 번도 착수 안 된 카드가 재계획에 잡힌다.

    이 카드는 세 조회 어디에도 안 걸리던 구멍이었다:
    - `list_scheduled_between` → `start_at >= 다음 주 월요일` 이라 과거 블록은 대상 밖
    - `list_planned_without_block` → 비-cancelled 블록을 갖고 있어 백로그 정의에서 빠짐
    - `expire_unreflected` cron → execution_events 기준이라 [▶시작] 안 한 카드는 영원히 안 걸림

    즉 "계획만 세워두고 그냥 안 한" 가장 흔한 실패 모드의 카드가 재계획에서 통째로 사라졌다.
    """
    _freeze_now(monkeypatch)  # 2026-07-09(목) 12:00
    stale = _seed_action(fake_action_item_repo, title="밀린 카드", target=date(2026, 7, 6))
    old = _seed_block(
        fake_scheduled_block_repo,
        action_id=stale.id,
        start=_kst(2026, 7, 6, 10, 0),  # 3일 전 — 한 번도 착수 안 함(status='scheduled')
        end=_kst(2026, 7, 6, 10, 30),
    )

    resp = client.post("/plans/replan")
    assert resp.status_code == 201, resp.text
    body = resp.json()

    by_action = {b["actionId"]: b for b in body["blocks"]}
    assert f"action_{stale.id}" in by_action, "밀린 카드가 재계획 후보에 잡혀야 한다"
    # 밀린 옛 블록이 '교체 대상'으로 실려야 승인 때 취소되고 새 블록으로 대체된다.
    # (payload 의 oldBlocks 는 응답에 노출되지 않으므로 replacesBlockId 로 확인한다.)
    assert by_action[f"action_{stale.id}"]["replacesBlockId"] == f"block_{old.id}"
    # 새 블록은 재배치 창(다음 주 월요일 이후)에 놓인다 — 과거에 다시 배치하지 않는다.
    assert by_action[f"action_{stale.id}"]["start"][:10] >= WINDOW_START.isoformat()


def test_generate_stale_card_started_is_left_alone(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> None:
    """과거 블록이라도 **착수한** 것은 건드리지 않는다 — 기존 불변 규칙이 밀린 일 회수보다 우선."""
    _freeze_now(monkeypatch)
    started = _seed_action(fake_action_item_repo, title="착수함", target=date(2026, 7, 6))
    _seed_block(
        fake_scheduled_block_repo,
        action_id=started.id,
        start=_kst(2026, 7, 6, 10, 0),
        end=_kst(2026, 7, 6, 10, 30),
        status="started",
    )

    resp = client.post("/plans/replan")
    assert resp.status_code == 201, resp.text
    by_action = {b["actionId"] for b in resp.json()["blocks"]}
    assert f"action_{started.id}" not in by_action


def test_generate_stale_user_edit_block_is_preserved(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> None:
    """사용자가 직접 옮긴 과거 블록도 불변(#113) — 밀린 일 회수가 이 원칙을 뚫지 않는다."""
    _freeze_now(monkeypatch)
    moved = _seed_action(fake_action_item_repo, title="직접 옮김", target=date(2026, 7, 6))
    _seed_block(
        fake_scheduled_block_repo,
        action_id=moved.id,
        start=_kst(2026, 7, 6, 10, 0),
        end=_kst(2026, 7, 6, 10, 30),
        source="user_edit",
    )

    resp = client.post("/plans/replan")
    assert resp.status_code == 201, resp.text
    by_action = {b["actionId"] for b in resp.json()["blocks"]}
    assert f"action_{moved.id}" not in by_action


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
    """확정(started) 블록 + 고정일정(#112 정합)을 busy 로 피해 배치한다.

    ⚠️ #117 리뷰에서 이 테스트가 **공허함**이 드러났다 — `api/routes/planning.py:1025`
    의 `committed.extend(fixed_schedules_to_busy(day, fixed))` 를 통째로 지워도 이
    테스트를 포함해 전 스위트(1095건 당시)가 그대로 통과했다. 원인은 기본 활동 시간
    (수면 23~08 제외 전부 열림)에서 30분짜리 액션 하나가 **첫날 08:00** 에 곧바로
    배치돼, 재배치 창의 확정 블록(07-14)에도 고정일정(13~18)에도 닿지 않았기 때문이다
    — 뒤에 붙인 겹침 검사 자체는 옳지만 검사할 대상이 애초에 생기지 않았다.

    그래서 재배치 창 **첫날(07-13)** 아침을 확정 블록(08~13)으로, 그 직후를 고정일정
    (13~18)으로 틈 없이 이어 붙였다. 두 가드가 **둘 다** 살아 있어야만 유일하게 남는
    빈틈(18~23)에 배치되고, 어느 한쪽이라도 빠지면 그 앞(08:00 또는 13:00)의 침범
    구간에 배치된다 — "몇 시에 배치됐는가" 하나로 두 가드를 동시에, 결정적으로 검증한다.
    """
    _freeze_now(monkeypatch)
    _seed_action(fake_action_item_repo, title="백로그", target=date(2026, 7, 16))
    # 확정(started) 블록 — 재배치 창 첫날(07-13) 08:00~13:00, 고정일정 시작과 맞닿는다.
    _seed_block(
        fake_scheduled_block_repo,
        action_id=uuid4(),
        start=_kst(2026, 7, 13, 8, 0),
        end=_kst(2026, 7, 13, 13, 0),
        status="started",
    )
    # 고정일정 매일 13:00~18:00 — 확정 블록 종료 시각에 바로 이어 붙는다(빈틈 없음).
    _seed_fixed(fake_fixed_schedule_repo, start=time(13, 0), end=time(18, 0))

    resp = client.post("/plans/replan")
    assert resp.status_code == 201, resp.text
    blocks = resp.json()["blocks"]
    assert blocks  # 적어도 하나는 배치

    first = min(blocks, key=lambda b: b["start"])
    start = datetime.fromisoformat(first["start"])
    # 두 가드가 모두 작동해야만 도달하는 유일한 빈틈 — 08:00 이면 확정 블록 가드가,
    # 13:00 이면 고정일정 가드가 사라졌다는 뜻이라 값 자체로 원인이 구분된다.
    assert start == _kst(2026, 7, 13, 18, 0), (
        f"08:00(확정 블록 침범) 또는 13:00(고정일정 침범) 이 아니라 "
        f"18:00 이어야 한다 — 실제: {start.time()}"
    )

    for b in blocks:
        start = datetime.fromisoformat(b["start"])
        end = datetime.fromisoformat(b["end"])
        # 확정 블록 구간(07-13 08~13)과 겹치지 않는다.
        assert not (start < _kst(2026, 7, 13, 13, 0) and end > _kst(2026, 7, 13, 8, 0))
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


def test_approve_moves_the_card_date_with_its_block(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """카드 날짜가 새 블록 날짜를 따라간다 (#229).

    #223 이 이 규칙을 `_apply_once`(승인 INSERT)와 `edit_block`(PATCH)엔 넣었지만 이
    경로(주간 forward 재계획 승인)는 빠뜨렸다 — 옛 블록을 cancel 하고 새 블록을 만들면서
    `action.target_date` 를 안 건드려, 배포 이후에도 재계획을 승인할 때마다 '카드 날짜
    ≠ 블록 날짜' 가 새로 생겼다(오늘 아젠다는 target_date 로만 조회하므로 그 카드는
    영영 어느 날의 오늘 탭에도 안 뜬다). 라이브 실측: 73건 중 상당수가 이 경로로 생겼다.
    """
    _freeze_now(monkeypatch)
    # 카드는 지난 계획의 날짜(7/9)에 멈춰 있고, 블록은 7/15 로 재배치된다.
    action_a = _seed_action(fake_action_item_repo, title="A", target=date(2026, 7, 9))
    old = _seed_block(
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 9, 10, 0),
        end=_kst(2026, 7, 9, 10, 30),
    )
    draft_id = _seed_replan_draft(
        fake_plan_draft_repo,
        blocks=[
            _pblock(
                action_id=action_a.id,
                start=_kst(2026, 7, 15, 8, 0),
                end=_kst(2026, 7, 15, 8, 30),
                replaces=old.id,
            )
        ],
    )

    resp = client.post(f"/plans/replan/{draft_id}/approve")
    assert resp.status_code == 200, resp.text
    assert action_a.target_date == date(2026, 7, 15), (
        f"카드가 여전히 옛 날짜다: {action_a.target_date} — 오늘 아젠다에서 영영 안 뜬다"
    )


def test_approve_uses_the_earliest_active_block_for_split_sessions(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """세션이 여러 날에 걸치면 카드 날짜는 **가장 이른** 활성 블록을 따른다.

    `edit_block`(planning.py) 과 `_apply_once`(first_plan_adapter.py)의 규칙을 그대로
    맞춘 것 — 마지막 세션 날짜를 쓰면 카드가 계획 기간 내내 오늘 아젠다에 뜨지 않는다.
    """
    _freeze_now(monkeypatch)
    action_a = _seed_action(fake_action_item_repo, title="A", target=date(2026, 7, 9))
    old1 = _seed_block(
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 9, 10, 0),
        end=_kst(2026, 7, 9, 10, 30),
    )
    old2 = _seed_block(
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 9, 14, 0),
        end=_kst(2026, 7, 9, 14, 30),
    )
    draft_id = _seed_replan_draft(
        fake_plan_draft_repo,
        blocks=[
            _pblock(
                action_id=action_a.id,
                start=_kst(2026, 7, 17, 8, 0),
                end=_kst(2026, 7, 17, 8, 30),
                replaces=old1.id,
            ),
            _pblock(
                action_id=action_a.id,
                start=_kst(2026, 7, 15, 9, 0),  # 더 이른 날 — 이게 카드 날짜여야 한다
                end=_kst(2026, 7, 15, 9, 30),
                replaces=old2.id,
            ),
        ],
    )

    resp = client.post(f"/plans/replan/{draft_id}/approve")
    assert resp.status_code == 200, resp.text
    assert action_a.target_date == date(2026, 7, 15)


def test_approve_does_not_move_the_date_when_the_action_is_skipped(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """옛 블록이 이미 시작됐으면 액션 전체가 skip 이다 — 카드 날짜도 손대면 안 된다."""
    _freeze_now(monkeypatch)
    action_a = _seed_action(fake_action_item_repo, title="A", target=date(2026, 7, 9))
    old = _seed_block(
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 9, 10, 0),
        end=_kst(2026, 7, 9, 10, 30),
        status="started",
    )
    draft_id = _seed_replan_draft(
        fake_plan_draft_repo,
        blocks=[
            _pblock(
                action_id=action_a.id,
                start=_kst(2026, 7, 15, 8, 0),
                end=_kst(2026, 7, 15, 8, 30),
                replaces=old.id,
            )
        ],
    )

    resp = client.post(f"/plans/replan/{draft_id}/approve")
    assert resp.status_code == 200, resp.text
    assert action_a.target_date == date(2026, 7, 9), "skip 된 액션의 날짜가 바뀌었다"


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


def test_approve_replaces_all_split_session_blocks(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """분할(다중 세션): 옛 블록 2개 + 새 세션 2개 → 옛 것 **전부** 취소·새 것 **전부** 생성.

    #115 스케줄러가 긴 액션을 여러 세션으로 쪼갠 경우. 액션당 옛 블록 1개만 재조정하면
    나머지가 유령으로 남거나(중복) 새 세션이 드롭(손실)되던 리뷰 지적을 봉합.
    """
    _freeze_now(monkeypatch)
    action_a = _seed_action(fake_action_item_repo, title="긴 작업")
    b1 = _seed_block(
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 15, 10, 0),
        end=_kst(2026, 7, 15, 10, 50),
    )
    b2 = _seed_block(
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 15, 11, 0),
        end=_kst(2026, 7, 15, 11, 50),
    )
    draft_id = _seed_replan_draft(
        fake_plan_draft_repo,
        old_blocks={f"action_{action_a.id}": [f"block_{b1.id}", f"block_{b2.id}"]},
        blocks=[
            _pblock(
                action_id=action_a.id,
                start=_kst(2026, 7, 14, 8, 0),
                end=_kst(2026, 7, 14, 8, 50),
                replaces=b1.id,
            ),
            _pblock(
                action_id=action_a.id,
                start=_kst(2026, 7, 14, 9, 0),
                end=_kst(2026, 7, 14, 9, 50),
                replaces=b1.id,
            ),
        ],
    )

    resp = client.post(f"/plans/replan/{draft_id}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["cancelledBlocks"], body["createdBlocks"], body["skippedBlocks"]) == (2, 2, 0)
    # 옛 블록 둘 다 취소(유령 없음), 새 scheduled 세션 2개.
    assert fake_scheduled_block_repo._blocks[b1.id].block_status == "cancelled"
    assert fake_scheduled_block_repo._blocks[b2.id].block_status == "cancelled"
    new = [
        b
        for b in fake_scheduled_block_repo._blocks.values()
        if b.action_item_id == action_a.id and b.block_status == "scheduled"
    ]
    assert len(new) == 2


def test_approve_preserves_split_action_when_one_session_started(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """분할 액션의 한 세션이 그새 started 면 액션 **전체 보존**(취소·생성 skip)."""
    _freeze_now(monkeypatch)
    action_a = _seed_action(fake_action_item_repo, title="긴 작업")
    b1 = _seed_block(
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 15, 10, 0),
        end=_kst(2026, 7, 15, 10, 50),
        status="started",
    )
    b2 = _seed_block(
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 15, 11, 0),
        end=_kst(2026, 7, 15, 11, 50),
    )
    draft_id = _seed_replan_draft(
        fake_plan_draft_repo,
        old_blocks={f"action_{action_a.id}": [f"block_{b1.id}", f"block_{b2.id}"]},
        blocks=[
            _pblock(
                action_id=action_a.id,
                start=_kst(2026, 7, 14, 8, 0),
                end=_kst(2026, 7, 14, 8, 50),
                replaces=b1.id,
            ),
            _pblock(
                action_id=action_a.id,
                start=_kst(2026, 7, 14, 9, 0),
                end=_kst(2026, 7, 14, 9, 50),
                replaces=b1.id,
            ),
        ],
    )

    resp = client.post(f"/plans/replan/{draft_id}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["cancelledBlocks"], body["createdBlocks"], body["skippedBlocks"]) == (0, 0, 2)
    # 착수한 액션은 옛 블록 둘 다 그대로 보존.
    assert fake_scheduled_block_repo._blocks[b1.id].block_status == "started"
    assert fake_scheduled_block_repo._blocks[b2.id].block_status == "scheduled"


def test_generate_captures_all_split_old_blocks(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """generate: 한 액션에 미래 scheduled 블록이 2개면 oldBlocks 맵이 **둘 다** 담아야 한다."""
    _freeze_now(monkeypatch)
    action_a = _seed_action(fake_action_item_repo, title="A")
    b1 = _seed_block(
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 15, 10, 0),
        end=_kst(2026, 7, 15, 10, 50),
    )
    b2 = _seed_block(
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 16, 10, 0),
        end=_kst(2026, 7, 16, 10, 50),
    )

    resp = client.post("/plans/replan")
    assert resp.status_code == 201, resp.text
    # 응답엔 대표 1개만 노출되지만, 저장된 draft payload 의 oldBlocks 는 둘 다 담아야 한다.
    draft = next(iter(fake_plan_draft_repo._items.values()))
    old_map = draft.payload["oldBlocks"]
    key = f"action_{action_a.id}"
    assert key in old_map
    assert {f"block_{b1.id}", f"block_{b2.id}"} == set(old_map[key])


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


# ── 리뷰 회귀 (#122 blocker) ─────────────────────────────────────────────────
# 아래는 전부 "CI green 인데도 실제로 깨지던" 것들이다. 리뷰가 지적했듯 기존 테스트에는
# user_edit 이 한 번도 등장하지 않아, repo 의 user_edit 필터를 양쪽 다 지워도 전부 통과했다.


def test_approve_preserves_block_user_moved_after_generate(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """generate 이후 사용자가 옮긴 블록(user_edit)을 approve 가 지우면 안 된다 (TOCTOU).

    회귀: generate 쪽 list_scheduled_between 의 user_edit 필터는 approve 보다 수 초~수 시간
    앞서 돈다. 그 사이 사용자가 HITL 검토 중 블록을 드래그하면 edit_block 이 source 만
    'user_edit' 으로 바꾸고 block_status 는 'scheduled' 로 남기는데, approve 가 status 만
    보고 취소해 **사용자가 손으로 옮긴 계획을 파괴**했다. edit_block 은 lock 을 안 잡아
    user_agent_lock 으로도 못 막는다 — 쓰기 시점에 source 를 다시 봐야 한다.
    """
    _freeze_now(monkeypatch)
    action_a = _seed_action(fake_action_item_repo, title="A")
    old = _seed_block(  # generate 시점엔 ai_plan 이라 후보로 잡혔다
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
    # HITL 검토 창에서 사용자가 직접 드래그 (edit_block 과 동일한 상태 전이).
    old.source = "user_edit"
    old.start_at = _kst(2026, 7, 16, 9, 0)
    old.end_at = _kst(2026, 7, 16, 9, 30)

    resp = client.post(f"/plans/replan/{draft_id}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["cancelledBlocks"], body["createdBlocks"], body["skippedBlocks"]) == (0, 0, 1)
    # 사용자가 옮긴 블록은 그대로 살아있고, 그 자리를 덮는 새 블록도 안 생긴다.
    assert fake_scheduled_block_repo._blocks[old.id].block_status == "scheduled"
    assert fake_scheduled_block_repo._blocks[old.id].start_at == _kst(2026, 7, 16, 9, 0)
    assert not [
        b
        for b in fake_scheduled_block_repo._blocks.values()
        if b.id != old.id and b.action_item_id == action_a.id and b.block_status == "scheduled"
    ]


def test_generate_skips_card_the_user_has_moved(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> None:
    """카드의 블록 중 user_edit 이 하나라도 있으면 그 카드는 통째로 보존한다 (#113 계약).

    회귀: replan 은 user_edit 을 블록 단위로만 걸러(list_scheduled_between) 같은 카드의
    다른 세션은 후보로 올렸다. first_plan_adapter.protected_card_ids 는 카드 단위로
    보존하므로 두 승인 경로의 '사용자가 건드린 것' 정의가 어긋났다.
    """
    _freeze_now(monkeypatch)
    action_a = _seed_action(fake_action_item_repo, title="분할카드", est=120)
    _seed_block(  # 사용자가 옮긴 세션
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 14, 9, 0),
        end=_kst(2026, 7, 14, 10, 0),
        source="user_edit",
    )
    _seed_block(  # 같은 카드의 AI 세션 — 예전엔 이것만 보고 후보로 올렸다
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 15, 9, 0),
        end=_kst(2026, 7, 15, 10, 0),
    )

    resp = client.post("/plans/replan")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert not [b for b in body["blocks"] if b["actionId"] == f"action_{action_a.id}"], (
        "사용자가 옮긴 카드는 재계획 후보가 되면 안 된다"
    )


def test_generate_does_not_double_schedule_action_across_week_boundary(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> None:
    """주 경계를 걸친 분할 액션을 이중 배치하면 안 된다 (120분 액션에 180분).

    회귀: 후보는 액션의 **전체** estimated_minutes 로 만들면서, 교체할 옛 블록은 스캔 창
    [window_start, +365d] 안에서만 모았다. 이번 주 블록은 '보존'되어 취소되지 않으므로
    살아남은 60분 + 새로 배치한 120분 = 180분이 된다. 세션 분할이 액션을 여러 날에 흩기
    때문에 레이스도 사용자 편집도 없이 일상적으로 발생한다.
    """
    _freeze_now(monkeypatch)  # 2026-07-09(목) → window_start=07-13(월)
    action_a = _seed_action(fake_action_item_repo, title="논문 읽기", est=120)
    _seed_block(  # 이번 주(창 밖) 미래 세션 — 보존되며 60분을 이미 차지한다
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 10, 9, 0),
        end=_kst(2026, 7, 10, 10, 0),
    )
    _seed_block(  # 다음 주(창 안) 세션 — 재배치 대상
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 15, 9, 0),
        end=_kst(2026, 7, 15, 10, 0),
    )

    resp = client.post("/plans/replan")
    assert resp.status_code == 201, resp.text
    mine = [b for b in resp.json()["blocks"] if b["actionId"] == f"action_{action_a.id}"]
    planned = sum(
        (datetime.fromisoformat(b["end"]) - datetime.fromisoformat(b["start"])).total_seconds() / 60
        for b in mine
    )
    # 살아남는 60분을 뺀 나머지 60분만 다시 배치해야 총량이 120분으로 유지된다.
    assert planned == 60, f"창 밖 세션 60분을 빼지 않아 {planned}분을 재배치했다"


def test_generate_preserves_action_with_started_sibling_session(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> None:
    """형제 세션을 이미 착수한 액션은 후보로 올리지 않는다 — 이미 한 일을 다시 시키지 않게.

    회귀: list_scheduled_between 이 'scheduled' 만 반환하므로 started 형제는 oldBlocks 에
    안 실렸고, approve 의 started/finished 가드도 발동하지 않았다. 결과적으로 착수한 60분
    위에 새 120분이 얹혀 총 180분이 됐다. generate 가드를 approve 가드와 같은 규칙으로 맞춘다.
    """
    _freeze_now(monkeypatch)
    action_a = _seed_action(fake_action_item_repo, title="캡스톤", est=120)
    _seed_block(
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 14, 10, 0),
        end=_kst(2026, 7, 14, 11, 0),
        status="started",
    )
    _seed_block(
        fake_scheduled_block_repo,
        action_id=action_a.id,
        start=_kst(2026, 7, 15, 9, 0),
        end=_kst(2026, 7, 15, 10, 0),
    )

    resp = client.post("/plans/replan")
    assert resp.status_code == 201, resp.text
    assert not [b for b in resp.json()["blocks"] if b["actionId"] == f"action_{action_a.id}"], (
        "착수한 액션은 재계획 후보가 되면 안 된다"
    )


def test_generate_draft_never_outlives_its_window_start(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """재계획 Draft 만료는 자기 window_start 를 넘지 못한다 — 과거 블록 생성 방지.

    회귀: 기본 TTL 72h 만 쓰면, 일요일 생성(next_week_start 가 '내일')한 draft 를 그 주가
    시작된 뒤 승인할 수 있었다. 그러면 살아있는 미래 블록을 취소하고 **과거 블록을 새로
    만든다**(멀쩡한 미래를 죽은 과거와 맞바꿈). 늦은 승인은 문서화된 410 으로 떨어져야 한다.
    """
    sunday = datetime(2026, 7, 19, 10, 0, tzinfo=KST)  # 일 → window_start = 07-20(월)
    _freeze_now(monkeypatch, sunday)
    _seed_action(fake_action_item_repo, title="A", est=30, target=date(2026, 7, 24))

    resp = client.post("/plans/replan")
    assert resp.status_code == 201, resp.text

    draft = next(iter(fake_plan_draft_repo._items.values()))
    window_start_dt = datetime(2026, 7, 20, 0, 0, tzinfo=KST)
    assert draft.expires_at <= window_start_dt, (
        f"만료 {draft.expires_at} 가 window_start {window_start_dt} 를 넘겨, "
        "그 주가 시작된 뒤 승인 → 과거 블록 생성이 가능하다"
    )


def test_get_plan_does_not_500_on_replan_draft(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
) -> None:
    """GET /plans/{id} 에 재계획 draft id 를 주면 500 이 아니라 문서화된 404 여야 한다.

    회귀: _draft_to_response 가 replan payload 에 없는 goal_nodes 를 읽어 uncaught KeyError
    → 500. FE 가 approve 전에 앱을 백그라운드로 보냈다 돌아오면 재현된다. 승인 경로에는
    같은 가드가 이미 있었고 get_plan 만 빠져 있었다.
    """
    _freeze_now(monkeypatch)
    action_a = _seed_action(fake_action_item_repo, title="A")
    draft_id = _seed_replan_draft(
        fake_plan_draft_repo,
        blocks=[
            _pblock(
                action_id=action_a.id,
                start=_kst(2026, 7, 14, 8, 0),
                end=_kst(2026, 7, 14, 8, 30),
            )
        ],
    )

    resp = client.get(f"/plans/{draft_id}")
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "PLAN_DRAFT_NOT_FOUND"


# ── 개인화 파리티 (세션 길이·선호 시간) ─────────────────────────────────────
# First Plan 이 넣은 goals.session_length / goals.preferred_time 를 재계획도 반영해야 한다.
# 안 그러면 매주 재계획 때마다 60분 청크·free-time 아무데나로 리셋된다(B 백로그).


def _finish_session(repo: FakeInterviewRepo) -> None:
    """정상 종료 인터뷰 세션 1개 시드 — get_latest_finished 가 outcome 복구를 시도하게."""
    from reaction_backend.db.models.interview_session import InterviewSession

    row = InterviewSession()
    row.id = uuid4()
    row.user_id = DEMO_USER_UUID
    row.kind = "plan"
    row.end_reason = "completed"
    row.total_turns = 5
    row.ambiguity_final = 0.1
    row.ended_at = FROZEN_NOW
    row.used_fallback = False
    repo._sessions[row.id] = row
    repo._answers[row.id] = {}


def _craft_outcome(*, session_min: int, preferred_time: str) -> InterviewOutcome:
    """heaviest 목표에 세션 길이·선호 시간을 실은 outcome(재계획 튜닝 유도 대상)."""
    return InterviewOutcome(
        session_id="iv_replan",
        generated_at=now_kst(),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대3", season="학기중"),
        core_goals=[
            GoalCandidate(
                title="논문 읽기",
                category="study",
                is_heaviest=True,
                tentative_tier="focus",
                confidence=0.9,
                session_length_min=session_min,
                preferred_time=preferred_time,
            )
        ],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="06:00", end="23:00"),
            peak_window=["저녁"],  # 전역 peak 은 저녁 — 목표별 preferred_time 이 이겨야 함
        ),
        preferences=PreferenceProfile(recovery_tone="담백", rest_ok=True, downscope_unit_min=10),
        horizon=None,
    )


def test_generate_replan_honors_session_length_and_preferred_time(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_interview_repo: FakeInterviewRepo,
) -> None:
    """재계획이 세션 길이(90분)를 60분으로 쪼개지 않고, 선호 시간(오전)에 배치한다.

    폴백(focus_chunk_min=60·peak=())이었다면 90분 액션이 60+30 으로 분할되고 저녁/free-time
    아무데나 놓인다. outcome 을 복구해 First Plan 과 같은 튜닝을 유도하면 90분 단일 세션이
    오전(06~12)에 놓인다.
    """
    _freeze_now(monkeypatch)
    _finish_session(fake_interview_repo)

    import reaction_backend.api.routes.planning as planning_mod

    async def _fake_project(row: Any, repo: Any) -> InterviewOutcome:
        return _craft_outcome(session_min=90, preferred_time="오전")

    monkeypatch.setattr(planning_mod, "_project_session_outcome", _fake_project)

    action = _seed_action(
        fake_action_item_repo, title="논문 읽기", est=90, target=date(2026, 7, 16)
    )

    resp = client.post("/plans/replan")
    assert resp.status_code == 201, resp.text
    mine = [b for b in resp.json()["blocks"] if b["actionId"] == f"action_{action.id}"]
    assert len(mine) == 1, f"90분 세션이 분할됐다: {mine}"
    start = datetime.fromisoformat(mine[0]["start"])
    end = datetime.fromisoformat(mine[0]["end"])
    assert (end - start).total_seconds() / 60 == 90, "세션 길이 90분이 유지되지 않았다"
    assert 6 <= start.hour < 12, f"선호 시간(오전)이 아닌 {start.hour}시에 배치됐다"


def test_generate_replan_falls_back_without_finished_interview(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_interview_repo: FakeInterviewRepo,
) -> None:
    """완료 인터뷰가 없으면(outcome 복구 불가) 기존 기본 튜닝으로 폴백해 여전히 배치된다."""
    _freeze_now(monkeypatch)  # 인터뷰 세션 미시드 → get_latest_finished 는 None
    action = _seed_action(fake_action_item_repo, title="백로그", est=45, target=date(2026, 7, 16))

    resp = client.post("/plans/replan")
    assert resp.status_code == 201, resp.text
    assert [b for b in resp.json()["blocks"] if b["actionId"] == f"action_{action.id}"]


def test_weekly_replan_approve_schema_name_does_not_collide_with_recovery() -> None:
    """OpenAPI 컴포넌트명이 회복 replan 과 충돌하면 안 된다 — FE 생성 클라이언트 보호.

    회귀: planning 에 recovery 와 **동명**인 ReplanApproveResponse 를 추가하자, FastAPI 가
    중복 모델명을 양쪽 다 full-qualify 로 바꿔(reaction_backend__schemas__recovery__...)
    이 변경이 건드리지도 않은 회복 endpoint(POST /replan/{executionId}/approve)의 컴포넌트명이
    바뀌었다. replan 테스트로는 잡히지 않아 FE 빌드에서야 터진다.
    """
    from reaction_backend.main import create_app

    schemas = create_app().openapi()["components"]["schemas"]
    qualified = [n for n in schemas if n.startswith("reaction_backend__schemas__")]
    assert not qualified, f"모델명 충돌로 full-qualify 된 컴포넌트가 있다: {qualified}"
    assert "ReplanApproveResponse" in schemas  # 회복 endpoint 의 이름이 그대로 유지된다
