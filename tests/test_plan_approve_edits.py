"""첫 계획 승인 — 초안 편집 반영(planA-2) + 재시도 루프의 만료 인스턴스(planA-3).

planA-2: 초안 화면에서 옮기고·지우고·이름 바꾼 것이 승인 때 버려지고 AI 원안이 저장됐다.
`POST /plans/{planId}/approve` 가 선택 본문 `blocks`(최종 블록 목록)를 받아 그대로 영속한다.
본문이 없거나 `{}` 이면 종전과 같다.

planA-3: 시도 실패 → rollback → 세션의 ORM 인스턴스 만료. 다음 시도가 `user.id` 를 읽다
MissingGreenlet 으로 터져 재시도도 PLAN_SAVE_FAILED 도 없이 일반 500 이 나갔다. 가짜 세션은
만료를 흉내내지 않아 기존 테스트가 이 경로를 못 봤다 — 여기서는 만료를 흉내낸다.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from reaction_backend.api.deps import get_current_user
from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.fixed_schedule import FixedSchedule
from reaction_backend.db.models.plan_draft import PlanDraft
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.integrations.google_calendar import freebusy
from reaction_backend.orchestrator.goal_structuring import BusyBlock, TimeInterval
from reaction_backend.orchestrator.plan_edit import (
    DraftBlockEdit,
    DraftEditError,
    apply_draft_edits,
)
from reaction_backend.schemas.common import KST, now_kst
from reaction_backend.schemas.planning import (
    ActionItemDraft,
    GoalNodeDraft,
    ScheduledBlockPreview,
)
from tests.conftest import (
    DEMO_USER_UUID,
    FakeFixedScheduleRepo,
    FakePlanDraftRepo,
    FakeScheduledBlockRepo,
)
from tests.test_planning_route import (
    _CapturingSession,
    _outcome,
    _RetryFailSession,
    _use_session,
)

_DAY = date(2026, 6, 22)  # 월요일 — 초안 블록이 놓인 날


def _at(hour: int, minute: int = 0, *, day: date = _DAY) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=KST)


def _preview(
    origin: str, start: datetime, *, dur: int = 60, title: str | None = None
) -> ScheduledBlockPreview:
    return ScheduledBlockPreview(
        start=start,
        end=start + timedelta(minutes=dur),
        title=title or f"작업 {origin}",
        category="study",
        origin="goal",
        origin_id=origin,
    )


def _draft_blocks() -> list[ScheduledBlockPreview]:
    # n1 은 두 회차로 나뉜 카드, n2 는 한 번짜리 카드.
    return [
        _preview("n1", _at(10), title="토익 LC (1/2)"),
        _preview("n1", _at(14), title="토익 LC (2/2)"),
        _preview("n2", _at(16), title="RC 파트 5"),
    ]


def _draft_actions() -> list[ActionItemDraft]:
    return [
        ActionItemDraft(
            node_id="n1",
            title="토익 LC",
            estimated_minutes=120,
            category="study",
            first_step="열기",
        ),
        ActionItemDraft(
            node_id="n2",
            title="RC 파트 5",
            estimated_minutes=60,
            category="study",
            first_step="풀기",
        ),
    ]


def _seed_two_card_draft(
    repo: FakePlanDraftRepo, blocks: list[ScheduledBlockPreview] | None = None
) -> UUID:
    nodes = [
        GoalNodeDraft(
            node_id="root",
            parent_id=None,
            title="토익",
            node_type="root",
            order_index=0,
            is_leaf=False,
        ),
        GoalNodeDraft(
            node_id="n1",
            parent_id="root",
            title="LC",
            node_type="leaf",
            order_index=0,
            is_leaf=True,
        ),
        GoalNodeDraft(
            node_id="n2",
            parent_id="root",
            title="RC",
            node_type="leaf",
            order_index=1,
            is_leaf=True,
        ),
    ]
    d = PlanDraft()
    d.id = uuid4()
    d.user_id = DEMO_USER_UUID
    d.status = "draft"
    d.target_date = _DAY
    d.horizon = None
    d.ai_source = "llm"
    d.payload = {
        "outcome": _outcome().model_dump(mode="json"),
        "goal_nodes": [n.model_dump(mode="json") for n in nodes],
        "action_items": [a.model_dump(mode="json") for a in _draft_actions()],
        "blocks": [b.model_dump(mode="json") for b in (blocks or _draft_blocks())],
        "warnings": [],
        "policy_violations": [],
        "generated_at": now_kst().isoformat(),
    }
    d.expires_at = now_kst() + timedelta(hours=1)
    d.approved_at = None
    repo._items[d.id] = d
    return d.id


def _edit(
    origin: str, start: datetime, *, dur: int = 60, title: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "originId": origin,
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=dur)).isoformat(),
    }
    if title is not None:
        body["title"] = title
    return body


def _persisted_blocks(cap: _CapturingSession) -> list[ScheduledBlock]:
    return [o for o in cap.added if isinstance(o, ScheduledBlock)]


def _persisted_actions(cap: _CapturingSession) -> list[ActionItem]:
    return [o for o in cap.added if isinstance(o, ActionItem)]


# ─────────────────────────── 순수 규칙 (apply_draft_edits) ───────────────────────────


def _dedit(
    origin: str, start: datetime, *, dur: int = 60, title: str | None = None
) -> DraftBlockEdit:
    return DraftBlockEdit(
        origin_id=origin, start=start, end=start + timedelta(minutes=dur), title=title
    )


def test_apply_edits_moves_drops_and_marks_only_moved_blocks() -> None:
    """n1 의 2회차를 옮기고 n2 를 목록에서 뺐다 → n2 카드는 안 만들고, 옮긴 것만 moved."""
    edited = apply_draft_edits(
        blocks=_draft_blocks(),
        action_items=_draft_actions(),
        edits=[_dedit("n1", _at(10)), _dedit("n1", _at(19))],
    )
    assert [(b.origin_id, b.start) for b in edited.blocks] == [("n1", _at(10)), ("n1", _at(19))]
    assert [b.start for b in edited.moved] == [_at(19)]
    assert [a.node_id for a in edited.action_items] == ["n1"]
    assert edited.dropped_origin_ids == ["n2"]


def test_apply_edits_session_suffix_is_not_a_rename_but_a_new_title_is() -> None:
    """초안 제목("토익 LC (1/2)")을 그대로 돌려보내면 개명이 아니다. 바꾼 제목은 카드 이름이
    되고, 회차 꼬리표는 떼고 저장한다."""
    same = apply_draft_edits(
        blocks=_draft_blocks(),
        action_items=_draft_actions(),
        edits=[
            _dedit("n1", _at(10), title="토익 LC (1/2)"),
            _dedit("n2", _at(16), title="RC 파트 5"),
        ],
    )
    assert [a.title for a in same.action_items] == ["토익 LC", "RC 파트 5"]

    renamed = apply_draft_edits(
        blocks=_draft_blocks(),
        action_items=_draft_actions(),
        edits=[_dedit("n1", _at(10), title="LC 받아쓰기 (1/2)"), _dedit("n2", _at(16))],
    )
    assert [a.title for a in renamed.action_items] == ["LC 받아쓰기", "RC 파트 5"]


def test_apply_edits_rejects_unknown_origin_and_inverted_time() -> None:
    with pytest.raises(DraftEditError) as unknown:
        apply_draft_edits(
            blocks=_draft_blocks(), action_items=_draft_actions(), edits=[_dedit("n9", _at(10))]
        )
    assert unknown.value.code == "COMMON_VALIDATION_ERROR"

    with pytest.raises(DraftEditError) as inverted:
        apply_draft_edits(
            blocks=_draft_blocks(),
            action_items=_draft_actions(),
            edits=[DraftBlockEdit(origin_id="n1", start=_at(11), end=_at(10))],
        )
    assert inverted.value.code == "PLAN_INVALID_TIME"


# 스케줄러가 실제로 만드는 15분 격자 밖 초안 — 수업(09:00~10:50) 직후 시작, 쉬는 시간 10분 뒤
# 2회차(:10 시작), 10분짜리 짧은 회차. 리뷰에서 재현된 세 경우를 한 초안에 담았다.
def _unaligned_draft_blocks() -> list[ScheduledBlockPreview]:
    return [
        _preview("n1", _at(10, 50), title="토익 LC (1/2)"),
        _preview("n2", _at(12, 40), dur=10, title="RC 파트 5"),
        _preview("n1", _at(13, 10), title="토익 LC (2/2)"),
    ]


def _unchanged_edits(blocks: list[ScheduledBlockPreview]) -> list[dict[str, Any]]:
    """FE 가 손대지 않은 초안을 그대로 돌려보내는 모양 — 받은 start/end·title 그대로."""
    return [
        {
            "originId": b.origin_id,
            "start": b.start.isoformat(),
            "end": b.end.isoformat(),
            "title": b.title,
        }
        for b in blocks
    ]


def test_apply_edits_keeps_unaligned_draft_times_and_marks_nothing_moved() -> None:
    """**리뷰 회귀.** 15분 격자 밖 초안을 그대로 보내면 초안 시각 그대로이고 옮긴 블록이 없다.

    예전엔 전부 15분 snap 한 뒤 초안과 비교해 10:50→10:45, 13:10→13:15, 12:40~12:50→
    12:45~12:45 가 됐고, 모두 "옮긴 블록"으로 잡혀 다시 검사됐다.
    """
    draft = _unaligned_draft_blocks()
    edited = apply_draft_edits(
        blocks=draft,
        action_items=_draft_actions(),
        edits=[DraftBlockEdit(b.origin_id or "", b.start, b.end, b.title) for b in draft],
    )
    assert [(b.start, b.end, b.title) for b in edited.blocks] == [
        (b.start, b.end, b.title) for b in draft
    ]
    assert edited.moved == []
    assert edited.dropped_origin_ids == []


def test_apply_edits_saves_a_moved_block_at_the_time_the_user_chose() -> None:
    """옮긴 블록은 보낸 시각 그대로(15분 격자로 당기지 않는다) — 초안 화면이 10:50 블록을
    한 시간 끌면 11:50 을 보여 주고, 사용자는 그 화면을 보고 승인한다. 초·밀리초는 뗀다."""
    edited = apply_draft_edits(
        blocks=_unaligned_draft_blocks(),
        action_items=_draft_actions(),
        edits=[
            DraftBlockEdit("n1", _at(11, 50).replace(second=30), _at(12, 50), "토익 LC (1/2)"),
            DraftBlockEdit("n2", _at(12, 40), _at(12, 50)),
        ],
    )
    assert [(b.start, b.end) for b in edited.moved] == [(_at(11, 50), _at(12, 50))]
    # 초안 그대로인 10분 회차는 10분 그대로 — 0분이 되거나 15분으로 늘지 않는다.
    assert [(b.start, b.end) for b in edited.blocks if b.origin_id == "n2"] == [
        (_at(12, 40), _at(12, 50))
    ]


def test_apply_edits_rejects_the_same_draft_block_sent_twice() -> None:
    """같은 초안 칸을 두 번 보내면 422 — 옮긴 블록이 아니라 겹침 검사를 안 거치므로 여기서
    막지 않으면 똑같은 블록 두 개가 저장된다."""
    with pytest.raises(DraftEditError) as dup:
        apply_draft_edits(
            blocks=_draft_blocks(),
            action_items=_draft_actions(),
            edits=[_dedit("n2", _at(16)), _dedit("n2", _at(16))],
        )
    assert dup.value.code == "COMMON_VALIDATION_ERROR"


# ─────────────────────────── 라우트 ───────────────────────────


def _seed_class(repo: FakeFixedScheduleRepo, days: list[str], start: time, end: time) -> None:
    s = FixedSchedule()
    s.id = uuid4()
    s.user_id = DEMO_USER_UUID
    s.title = "자료구조 수업"
    s.days_of_week = days
    s.start_time = start
    s.end_time = end
    s.archived_at = None
    repo._items[s.id] = s


def test_approve_unchanged_unaligned_draft_saves_the_exact_draft_times(
    client: TestClient,
    fake_plan_draft_repo: FakePlanDraftRepo,
    fake_fixed_schedule_repo: FakeFixedScheduleRepo,
) -> None:
    """**리뷰 회귀(라우트).** 수업(월 09:00~10:50) 직후 10:50 블록, 쉬는 시간 뒤 13:10 회차,
    10분짜리 12:40~12:50 회차를 손대지 않고 그대로 승인 → 200, 초안 시각 그대로 저장.

    예전엔 10:50 이 10:45 로 당겨져 422 PLAN_BLOCK_CONFLICT("…'자료구조 수업' 고정 일정과
    겹쳐요"), 10분 회차는 12:45~12:45 가 돼 422 PLAN_INVALID_TIME 이었다.
    """
    cap = _CapturingSession()
    _use_session(client, cap)
    _seed_class(fake_fixed_schedule_repo, ["mon"], time(9, 0), time(10, 50))
    draft = _unaligned_draft_blocks()
    plan_id = _seed_two_card_draft(fake_plan_draft_repo, draft)

    res = client.post(f"/plans/{plan_id}/approve", json={"blocks": _unchanged_edits(draft)})

    assert res.status_code == 200, res.text
    assert res.json()["activatedBlocks"] == 3
    saved = sorted((b.start_at, b.end_at) for b in _persisted_blocks(cap))
    assert saved == sorted((b.start, b.end) for b in draft)
    stored = client.get(f"/plans/{plan_id}").json()
    assert [(b["start"], b["end"]) for b in stored["blocks"]] == [
        (b.start.isoformat(), b.end.isoformat()) for b in draft
    ]


def test_approve_moved_unaligned_block_is_saved_where_the_user_put_it(
    client: TestClient,
    fake_plan_draft_repo: FakePlanDraftRepo,
    fake_fixed_schedule_repo: FakeFixedScheduleRepo,
) -> None:
    """월 10:50 블록을 화요일 같은 시각(화요일도 10:50 에 끝나는 수업)으로 옆으로 끌었다.
    15분 snap 했다면 10:45 가 돼 수업과 겹친다며 422 였다 — 사용자가 본 10:50 그대로 200."""
    cap = _CapturingSession()
    _use_session(client, cap)
    _seed_class(fake_fixed_schedule_repo, ["mon", "tue"], time(9, 0), time(10, 50))
    draft = _unaligned_draft_blocks()
    plan_id = _seed_two_card_draft(fake_plan_draft_repo, draft)
    tue = _DAY + timedelta(days=1)
    edits = _unchanged_edits(draft)
    edits[0] = _edit("n1", _at(10, 50, day=tue), title="토익 LC (1/2)")

    res = client.post(f"/plans/{plan_id}/approve", json={"blocks": edits})

    assert res.status_code == 200, res.text
    saved = sorted((b.start_at, b.end_at) for b in _persisted_blocks(cap))
    assert (_at(10, 50, day=tue), _at(11, 50, day=tue)) in saved
    assert (_at(12, 40), _at(12, 50)) in saved


def test_approve_persists_the_edited_blocks_not_the_ai_draft(
    client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo
) -> None:
    """**핵심 회귀.** 옮긴 시각으로 저장되고, 지운 카드는 안 만들어지고, 바꾼 이름이 저장된다."""
    cap = _CapturingSession()
    _use_session(client, cap)
    plan_id = _seed_two_card_draft(fake_plan_draft_repo)

    res = client.post(
        f"/plans/{plan_id}/approve",
        json={
            "blocks": [
                _edit("n1", _at(10), title="LC 받아쓰기 (1/2)"),
                _edit("n1", _at(19)),  # 14:00 → 19:00 으로 옮김
            ]
        },
    )

    assert res.status_code == 200, res.text
    assert res.json()["activatedActionItems"] == 1
    assert res.json()["activatedBlocks"] == 2
    blocks = _persisted_blocks(cap)
    assert sorted(b.start_at for b in blocks) == [_at(10), _at(19)]
    actions = _persisted_actions(cap)
    assert [a.title for a in actions] == ["LC 받아쓰기"]
    # 저장한 그대로를 초안 스냅샷에도 남긴다 — 재조회가 실제 저장본과 같아야 한다.
    stored = client.get(f"/plans/{plan_id}").json()
    assert [b["start"] for b in stored["blocks"]] == [_at(10).isoformat(), _at(19).isoformat()]


def test_approve_with_empty_body_keeps_the_draft(
    client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo
) -> None:
    """FE 가 지금 보내는 `{}` 는 종전과 같다 — 초안 그대로 3블록 2카드."""
    cap = _CapturingSession()
    _use_session(client, cap)
    plan_id = _seed_two_card_draft(fake_plan_draft_repo)

    res = client.post(f"/plans/{plan_id}/approve", json={})

    assert res.status_code == 200, res.text
    assert sorted(b.start_at for b in _persisted_blocks(cap)) == [_at(10), _at(14), _at(16)]
    assert len(_persisted_actions(cap)) == 2


def test_approve_rejects_an_origin_that_is_not_in_the_draft(
    client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo
) -> None:
    cap = _CapturingSession()
    _use_session(client, cap)
    plan_id = _seed_two_card_draft(fake_plan_draft_repo)

    res = client.post(f"/plans/{plan_id}/approve", json={"blocks": [_edit("gen-3", _at(10))]})

    assert res.status_code == 422
    assert res.json()["code"] == "COMMON_VALIDATION_ERROR"
    assert res.json()["field"] == "blocks"
    assert _persisted_blocks(cap) == []
    assert fake_plan_draft_repo._items[UUID(str(plan_id))].status == "draft"


def test_approve_rejects_a_block_moved_into_sleep(
    client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo
) -> None:
    """활동 시간(09~23) 밖 새벽 2시로 옮김 → 영속화 가드가 롤백, 422 PLAN_POLICY_VIOLATION."""
    cap = _CapturingSession()
    _use_session(client, cap)
    plan_id = _seed_two_card_draft(fake_plan_draft_repo)

    res = client.post(
        f"/plans/{plan_id}/approve",
        json={"blocks": [_edit("n1", _at(2)), _edit("n2", _at(16))]},
    )

    assert res.status_code == 422, res.text
    assert res.json()["code"] == "PLAN_POLICY_VIOLATION"
    assert cap.committed is False


def test_approve_rejects_a_block_moved_onto_an_existing_block(
    client: TestClient,
    fake_plan_draft_repo: FakePlanDraftRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> None:
    """다른 목표의 살아 있는 블록(19~20시) 위로 옮김 → 422 PLAN_BLOCK_CONFLICT."""
    _use_session(client, _CapturingSession())
    other = ScheduledBlock()
    other.id = uuid4()
    other.user_id = DEMO_USER_UUID
    other.action_item_id = uuid4()
    other.start_at = _at(19)
    other.end_at = _at(20)
    other.block_status = "scheduled"
    other.source = "ai_plan"
    other.external_calendar_event_id = None
    fake_scheduled_block_repo.seed(other, title="다른 목표", category="study")
    plan_id = _seed_two_card_draft(fake_plan_draft_repo)

    res = client.post(
        f"/plans/{plan_id}/approve",
        json={"blocks": [_edit("n1", _at(19, 30)), _edit("n2", _at(16))]},
    )

    assert res.status_code == 422, res.text
    assert res.json()["code"] == "PLAN_BLOCK_CONFLICT"


def test_approve_rejects_a_block_moved_onto_a_fixed_schedule(
    client: TestClient,
    fake_plan_draft_repo: FakePlanDraftRepo,
    fake_fixed_schedule_repo: FakeFixedScheduleRepo,
) -> None:
    """월 18~20 수업 위로 옮김 → 422 PLAN_BLOCK_CONFLICT, 문구에 수업 이름."""
    _use_session(client, _CapturingSession())
    s = FixedSchedule()
    s.id = uuid4()
    s.user_id = DEMO_USER_UUID
    s.title = "자료구조 수업"
    s.days_of_week = ["mon"]
    s.start_time = time(18, 0)
    s.end_time = time(20, 0)
    s.archived_at = None
    fake_fixed_schedule_repo._items[s.id] = s
    plan_id = _seed_two_card_draft(fake_plan_draft_repo)

    res = client.post(
        f"/plans/{plan_id}/approve",
        json={"blocks": [_edit("n1", _at(18, 30)), _edit("n2", _at(16))]},
    )

    assert res.status_code == 422, res.text
    assert res.json()["code"] == "PLAN_BLOCK_CONFLICT"
    assert "자료구조 수업" in res.json()["message"]


def _stub_calendar(
    monkeypatch: Any, status: str, by_day: dict[date, list[BusyBlock]] | None = None
) -> None:
    """`freebusy.fetch_busy_by_day` 를 대신한다 — 실제 구글 왕복 없이 busy 만 준다."""

    async def _fetch(session: Any, *, user_id: Any, start_day: date, end_day: date) -> Any:
        return (by_day or {}), status

    monkeypatch.setattr(freebusy, "fetch_busy_by_day", _fetch)


def test_approve_rejects_a_block_moved_onto_a_calendar_event(
    monkeypatch: Any, client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo
) -> None:
    """월 18~20 구글 약속 위로 옮김 → 422. 생성이 피하는 약속은 승인도 피한다(리뷰 반영)."""
    _use_session(client, _CapturingSession())
    _stub_calendar(
        monkeypatch,
        "ok",
        {_DAY: [BusyBlock(TimeInterval(_at(18), _at(20)), "calendar", "캘린더 일정")]},
    )
    plan_id = _seed_two_card_draft(fake_plan_draft_repo)

    res = client.post(
        f"/plans/{plan_id}/approve",
        json={"blocks": [_edit("n1", _at(18, 30)), _edit("n2", _at(16))]},
    )

    assert res.status_code == 422, res.text
    assert res.json()["code"] == "PLAN_BLOCK_CONFLICT"
    assert "캘린더" in res.json()["message"]
    assert "캘린더 일정" not in res.json()["message"]  # busy 조회라 약속 제목은 모른다


def test_approve_does_not_fail_when_the_calendar_lookup_fails(
    monkeypatch: Any, client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo
) -> None:
    """캘린더 조회가 실패해도 승인은 진행된다 — 생성과 같은 규칙(장애가 승인을 막지 않는다)."""
    _use_session(client, _CapturingSession())
    _stub_calendar(monkeypatch, "failed")
    plan_id = _seed_two_card_draft(fake_plan_draft_repo)

    res = client.post(
        f"/plans/{plan_id}/approve",
        json={"blocks": [_edit("n1", _at(18, 30)), _edit("n2", _at(16))]},
    )

    assert res.status_code == 200, res.text


def test_approve_rejects_two_edited_blocks_moved_onto_each_other(
    client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo
) -> None:
    _use_session(client, _CapturingSession())
    plan_id = _seed_two_card_draft(fake_plan_draft_repo)

    res = client.post(
        f"/plans/{plan_id}/approve",
        json={"blocks": [_edit("n1", _at(16, 30)), _edit("n2", _at(16))]},
    )

    assert res.status_code == 422, res.text
    assert res.json()["code"] == "PLAN_BLOCK_CONFLICT"


# ─────────────────────────── planA-3: 재시도 + 만료된 user ───────────────────────────


class _ExpiringUser:
    """rollback 뒤 만료된 ORM 인스턴스 흉내 — 속성을 읽으면 터지고, refresh 하면 풀린다.

    실 AsyncSession 에선 만료된 인스턴스의 속성 접근이 동기 지연 로드를 시도해
    MissingGreenlet 을 던진다. 가짜 세션은 아무것도 만료시키지 않아 이 경로가 안 보였다.
    """

    def __init__(self, real: User) -> None:
        self.__dict__["_real"] = real
        self.__dict__["expired"] = False

    def __getattr__(self, name: str) -> Any:
        if self.__dict__["expired"]:
            raise RuntimeError(f"MissingGreenlet: expired instance attribute {name!r}")
        return getattr(self.__dict__["_real"], name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "expired":
            self.__dict__["expired"] = value
            return
        setattr(self.__dict__["_real"], name, value)


class _ExpiringSession(_RetryFailSession):
    def __init__(self, user: _ExpiringUser, *, fail_times: int) -> None:
        super().__init__(fail_times=fail_times)
        self.user = user
        self.refreshed = 0

    async def rollback(self) -> None:
        await super().rollback()
        self.user.expired = True

    async def refresh(self, obj: Any) -> None:
        self.refreshed += 1
        if obj is self.user:
            self.user.expired = False


def _use_expiring_user(client: TestClient, demo_user_orm: User, *, fail_times: int) -> Any:
    user = _ExpiringUser(demo_user_orm)
    session = _ExpiringSession(user, fail_times=fail_times)
    _use_session(client, session)
    client.app.dependency_overrides[get_current_user] = lambda: user  # type: ignore[attr-defined]
    return session


def test_approve_retries_after_rollback_expired_the_user(
    client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo, demo_user_orm: User
) -> None:
    """한 번 실패(rollback → user 만료) 후 재시도가 실제로 돌아 200 — 온보딩 전이까지."""
    demo_user_orm.onboarding_state = "ONBOARDING_FIRST_PLAN"
    session = _use_expiring_user(client, demo_user_orm, fail_times=1)
    plan_id = _seed_two_card_draft(fake_plan_draft_repo)

    res = client.post(f"/plans/{plan_id}/approve")

    assert res.status_code == 200, res.text
    assert session.refreshed >= 1
    assert demo_user_orm.onboarding_state == "ACTIVE"


def test_approve_exhausted_retries_return_plan_save_failed_not_internal_error(
    client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo, demo_user_orm: User
) -> None:
    """세 번 다 실패하면 문서화된 500 PLAN_SAVE_FAILED — 일반 COMMON_INTERNAL_ERROR 가 아니다."""
    _use_expiring_user(client, demo_user_orm, fail_times=99)
    plan_id = _seed_two_card_draft(fake_plan_draft_repo)

    res = client.post(f"/plans/{plan_id}/approve")

    assert res.status_code == 500
    assert res.json()["code"] == "PLAN_SAVE_FAILED"
