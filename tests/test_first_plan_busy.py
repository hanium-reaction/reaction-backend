"""#118-3 통합 테스트 — DB busy(기존 블록 + 고정일정 + 시간정책)가 실제로 스케줄러에
도달해 회피되는지, `first_plan.schedule_blocks` 노드를 통해 검증한다.

기존 라우트 테스트의 `_FakeSession.execute` 는 항상 `[]` 라, `_existing_busy_by_day` /
`_fixed_schedules` / `_db_time_policies` 가 실 busy 를 스케줄러에 넣는 경로가 한 번도 안
돌았다. 여기서는 쿼리 대상 테이블별로 시드 행을 돌려주는 fake session 으로 그 경로를 태운다.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4

from reaction_backend.db.models.fixed_schedule import FixedSchedule
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.time_policy import TimePolicy
from reaction_backend.orchestrator import first_plan
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)
from reaction_backend.schemas.planning import ActionItemDraft, GoalDecomposition, GoalNodeDraft
from tests.conftest import DEMO_USER_UUID, _FakeResult, _FakeSession

KST = timezone(timedelta(hours=9))
TUE = date(2026, 7, 14)  # 화요일
THU = date(2026, 7, 16)


def _at(d: date, h: int, m: int = 0) -> datetime:
    return datetime.combine(d, time(h, m), tzinfo=KST)


class _RoutingSession(_FakeSession):
    """쿼리 대상 테이블별로 시드 행을 돌려주는 fake session — 실 busy 를 스케줄러까지 흘린다."""

    def __init__(
        self,
        *,
        blocks: list[ScheduledBlock],
        fixed: list[FixedSchedule],
        policies: list[TimePolicy],
    ) -> None:
        super().__init__()
        self._blocks = blocks
        self._fixed = fixed
        self._policies = policies

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:  # noqa: ARG002
        sql = str(stmt).lower()
        # superseded_card_ids(재생성 교체대상) — 없음(시드 미교체) → busy 유지.
        if "action_items" in sql:
            return _FakeResult([])
        if "fixed_schedules" in sql:
            return _FakeResult(self._fixed)
        if "time_policies" in sql:
            return _FakeResult(self._policies)
        if "scheduled_blocks" in sql:
            return _FakeResult(self._blocks)
        return _FakeResult([])


def _seed_block(day: date, sh: int, eh: int) -> ScheduledBlock:
    b = ScheduledBlock()
    b.id = uuid4()
    b.user_id = DEMO_USER_UUID
    b.action_item_id = uuid4()
    b.start_at = _at(day, sh)
    b.end_at = _at(day, eh)
    b.block_status = "scheduled"
    b.source = "ai_plan"
    return b


def _seed_fixed(days: list[str], sh: int, eh: int, title: str) -> FixedSchedule:
    f = FixedSchedule()
    f.id = uuid4()
    f.user_id = DEMO_USER_UUID
    f.title = title
    f.days_of_week = days
    f.start_time = time(sh, 0)
    f.end_time = time(eh, 0)
    return f


def _seed_policy(policy_type: str, payload: dict[str, str]) -> TimePolicy:
    p = TimePolicy()
    p.id = uuid4()
    p.user_id = DEMO_USER_UUID
    p.policy_type = policy_type
    p.payload = payload
    p.is_active = True
    return p


def _outcome() -> InterviewOutcome:
    return InterviewOutcome(
        session_id="t",
        generated_at=datetime.now(KST),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대3", season="학기중"),
        core_goals=[
            GoalCandidate(
                title="프로젝트",
                category="study",
                is_heaviest=True,
                tentative_tier="focus",
                confidence=0.9,
                deadline="2026-07-16",
            )
        ],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:30"), peak_window=["오후"]
        ),
        preferences=PreferenceProfile(recovery_tone="담백", rest_ok=True, downscope_unit_min=10),
        horizon="2026-07-16",
    )


def _state() -> Any:
    state = first_plan.initial_state(
        user_id=DEMO_USER_UUID, outcome=_outcome(), target_date=TUE.isoformat(), scope="horizon"
    )
    gp = GoalDecomposition(
        goal_nodes=[
            GoalNodeDraft(
                node_id="n1",
                parent_id=None,
                title="root",
                node_type="root",
                order_index=0,
                is_leaf=True,
            )
        ],
        action_items=[
            ActionItemDraft(
                node_id="n1",
                title=f"작업{i}",
                estimated_minutes=50,
                category="study",
                first_step="시작",
            )
            for i in range(3)
        ],
        policy_violations=[],
    )
    return {**state, "goal_plan": gp}


def _overlaps(bstart: datetime, bend: datetime, wstart: datetime, wend: datetime) -> bool:
    return bstart < wend and wstart < bend


async def test_schedule_blocks_avoids_db_busy_all_three_sources() -> None:
    """기존 블록 + 고정일정(수업) + DB 정책(점심)이 스케줄러까지 도달해 회피된다."""
    session = _RoutingSession(
        blocks=[_seed_block(TUE, 13, 15)],  # 기존 계획 블록 화 13:00~15:00
        fixed=[_seed_fixed(["tue", "thu"], 10, 12, "전공 수업")],  # 화·목 10:00~12:00
        policies=[_seed_policy("lunch", {"start_time": "12:00", "end_time": "13:00"})],  # 매일 점심
    )
    config: Any = {"configurable": {"session": session, "tone_mode": None}}

    new_state = await first_plan.schedule_blocks(_state(), config)
    blocks = new_state["scheduled_blocks"]
    assert blocks, "블록이 하나는 배치돼야 한다"

    for b in blocks:
        bs = b.start.astimezone(KST)
        be = b.end.astimezone(KST)
        wk = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[bs.weekday()]
        # 점심(매일 12~13) 회피
        assert not _overlaps(bs, be, _at(bs.date(), 12), _at(bs.date(), 13)), f"점심 겹침: {bs}"
        # 수업(화·목 10~12) 회피
        if wk in ("tue", "thu"):
            assert not _overlaps(bs, be, _at(bs.date(), 10), _at(bs.date(), 12)), f"수업 겹침: {bs}"
        # 기존 블록(화 13~15) 회피
        if bs.date() == TUE:
            assert not _overlaps(bs, be, _at(TUE, 13), _at(TUE, 15)), f"기존 블록 겹침: {bs}"


async def test_schedule_blocks_no_db_busy_uses_full_window() -> None:
    """DB busy 가 비면(빈 세션) outcome 활동창만으로 배치 — 회피 로직이 no-op."""
    session = _RoutingSession(blocks=[], fixed=[], policies=[])
    config: Any = {"configurable": {"session": session, "tone_mode": None}}
    new_state = await first_plan.schedule_blocks(_state(), config)
    assert len(new_state["scheduled_blocks"]) == 3  # 3 액션 전부 배치(막는 busy 없음)
