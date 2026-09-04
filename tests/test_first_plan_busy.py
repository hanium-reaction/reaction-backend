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
from reaction_backend.orchestrator import first_plan, first_plan_adapter, plan_scheduler
from reaction_backend.orchestrator.goal_structuring import (
    BusyBlock,
    DraftScheduledBlock,
    TimeInterval,
    pad_busy,
)
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


def _freq_state(*, deadline: str | None = "2026-08-01", sessions: int = 7) -> Any:
    """'매일'(frequency=7) 목표 + N개 세션 leaf — 요일 분산을 end-to-end 로 검증하기 위한 상태.

    deadline=None 이면 습관형(마감 없음) 코너 — _schedule_end 가 창을 하루로 붕괴시키는 경로.
    sessions 를 rate(7)의 배수가 아니게 주면 배치 창 올림 경로를 탄다.
    """
    outcome = InterviewOutcome(
        session_id="t-freq",
        generated_at=datetime.now(KST),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대3", season="학기중"),
        core_goals=[
            GoalCandidate(
                title="아침 운동",
                category="health",
                is_heaviest=True,
                tentative_tier="focus",
                confidence=0.9,
                session_length_min=50,
                frequency_per_week=7,  # 매일
                deadline=deadline,
            )
        ],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="06:00", end="23:30"), peak_window=["오전"]
        ),
        preferences=PreferenceProfile(recovery_tone="담백", rest_ok=True, downscope_unit_min=10),
        horizon=deadline,
    )
    state = first_plan.initial_state(
        user_id=DEMO_USER_UUID, outcome=outcome, target_date=TUE.isoformat(), scope="horizon"
    )
    gp = GoalDecomposition(
        goal_nodes=[
            GoalNodeDraft(
                node_id="n1",
                parent_id=None,
                title="root",
                node_type="root",
                order_index=0,
                is_leaf=False,
            ),
            *(
                GoalNodeDraft(
                    node_id=f"l{i}",
                    parent_id="n1",
                    title=f"운동 {i + 1}회차",
                    node_type="leaf",
                    order_index=i,
                    is_leaf=True,
                )
                for i in range(sessions)
            ),
        ],
        action_items=[
            ActionItemDraft(
                node_id=f"l{i}",
                title=f"운동 {i + 1}회차",
                estimated_minutes=50,
                category="health",
                first_step="스트레칭 5분",
            )
            for i in range(sessions)
        ],
        policy_violations=[],
    )
    return {**state, "goal_plan": gp}


async def test_schedule_blocks_daily_frequency_spreads_across_seven_days() -> None:
    """'매일'(frequency=7) → 7개 세션이 한 주(weeks_needed=1) 안 **서로 다른 7일**에 분산된다.

    회귀 방지: '매일 운동' 이 주 1일로만 몰리던 문제. frequency 가 주당 rate=7 → schedule_blocks
    의 weeks_needed=ceil(7/7)=1 로 배치 창을 한 주로 좁히고, 스케줄러 stride 가 요일마다 하나씩 편다.
    """
    session = _RoutingSession(blocks=[], fixed=[], policies=[])
    config: Any = {"configurable": {"session": session, "tone_mode": None}}
    new_state = await first_plan.schedule_blocks(_freq_state(), config)
    blocks = new_state["scheduled_blocks"]
    assert len(blocks) == 7, "7개 세션이 모두 배치돼야 한다"
    distinct_days = {b.start.astimezone(KST).date() for b in blocks}
    assert len(distinct_days) == 7, (
        f"서로 다른 7일에 분산돼야 하는데 {len(distinct_days)}일에 몰렸다"
    )
    # 배치 창이 한 주(TUE~+6일)로 좁혀졌는지 — 먼 마감(08-01)까지 흩뿌리지 않는다.
    assert max(distinct_days) <= TUE + timedelta(days=6)


async def test_schedule_blocks_daily_frequency_spreads_even_without_deadline() -> None:
    """마감 **없는** '매일' 습관도 7일에 분산된다 — 배치 창 하루-붕괴 회귀 봉합.

    회귀(시나리오 프로브로 발견): 마감 없는 습관형 목표는 _schedule_end(horizon=None)가 배치
    창을 target_date **하루**로 붕괴시켜, 주당 rate 만큼의 세션이 전부 첫날에 몰렸다('매일'이
    '하루 몰빵'). 운동·영어 같은 습관은 대개 마감이 없어 정작 빈도 기능의 주 용도에서 깨졌다.
    schedule_blocks 가 마감 없는 horizon 계획에서 density_end(weeks_needed 주)로 창을 펴야 한다.
    """
    session = _RoutingSession(blocks=[], fixed=[], policies=[])
    config: Any = {"configurable": {"session": session, "tone_mode": None}}
    new_state = await first_plan.schedule_blocks(_freq_state(deadline=None), config)
    blocks = new_state["scheduled_blocks"]
    assert len(blocks) == 7, "7개 세션이 모두 배치돼야 한다"
    distinct_days = {b.start.astimezone(KST).date() for b in blocks}
    assert len(distinct_days) == 7, (
        f"마감 없어도 서로 다른 7일에 분산돼야 하는데 {len(distinct_days)}일에 몰렸다"
    )
    # 마감이 없어도 무한 미래로 흩뿌리지 않고 한 주(days_needed ≤ 7)로 바운드된다.
    assert max(distinct_days) <= TUE + timedelta(days=6)


async def test_daily_frequency_stays_daily_when_sessions_are_not_a_week_multiple() -> None:
    """세션 수가 주(rate)의 배수가 아니어도 '매일'은 **매일**로 남는다.

    실측 회귀(로컬 E2E): 마감 8/15 + '매일'(rate 7) 인데 분해가 8세션만 나왔다. 배치 창을
    '필요한 주 수'로 올림하면 ceil(8/7)=2주=14일이라, stride 가 8세션을 14일에 흩뿌려
    **격일**(주 4회)이 됐다. 사용자가 고른 케이던스가 조용히 반토막 난 것.
    일 단위로 환산하면 ceil(8×7/7)=8일 → 연속 8일, 하루 1개가 유지된다.
    """
    session = _RoutingSession(blocks=[], fixed=[], policies=[])
    config: Any = {"configurable": {"session": session, "tone_mode": None}}
    new_state = await first_plan.schedule_blocks(
        _freq_state(deadline="2026-08-15", sessions=8), config
    )
    blocks = new_state["scheduled_blocks"]
    assert len(blocks) == 8, "8개 세션이 모두 배치돼야 한다"
    distinct_days = {b.start.astimezone(KST).date() for b in blocks}
    assert len(distinct_days) == 8, f"8일에 하루씩 배치돼야 하는데 {len(distinct_days)}일에 몰렸다"
    # 창이 정확히 8일 — 예전 주 단위 올림(14일)이면 빈 날이 6일 생겼다.
    assert max(distinct_days) - min(distinct_days) == timedelta(days=7), (
        f"연속 8일이어야 하는데 {max(distinct_days) - min(distinct_days)} 에 퍼졌다"
    )


# ── #231 이미 지난 마감 ───────────────────────────────────────────────────


async def test_past_deadline_does_not_collapse_the_placement_window() -> None:
    """이미 지난 마감이어도 세션이 여러 날에 퍼진다 — 하루 몰빵 + 배치 실패 경고 회귀 봉합.

    회귀(코너 배터리 실측, 실 LLM): "마감이 지났는데 아직 못 냈어요" + 마감 2026-08-01 을
    인터뷰가 그대로 받아, `_schedule_end` 의 `max(end, start_day)` 가 배치 창을 **오늘 하루**로
    붕괴시켰다. 3세션 중 1개만 들어가고 2개는 "'…' 을(를) 배치할 가용 시간을 찾지 못했어요"
    경고로 남아, 사용자에겐 계획을 만들다 만 것으로 보였다.
    """
    session = _RoutingSession(blocks=[], fixed=[], policies=[])
    config: Any = {"configurable": {"session": session, "tone_mode": None}}
    past = (TUE - timedelta(days=11)).isoformat()  # 2026-07-03 — 11일 지난 마감
    new_state = await first_plan.schedule_blocks(_freq_state(deadline=past), config)
    blocks = new_state["scheduled_blocks"]
    assert len(blocks) == 7, "7개 세션이 모두 배치돼야 한다"
    distinct_days = {b.start.astimezone(KST).date() for b in blocks}
    assert len(distinct_days) == 7, (
        f"지난 마감이어도 7일에 분산돼야 하는데 {len(distinct_days)}일에 몰렸다"
    )
    assert max(distinct_days) <= TUE + timedelta(days=6), "따라잡기 창은 한 주로 바운드"
    assert not [w for w in new_state["schedule_warnings"] if "가용 시간을 찾지 못했" in w], (
        "창이 펴졌으면 배치 실패 경고가 없어야 한다"
    )


async def test_past_deadline_is_disclosed_in_warnings() -> None:
    """지난 마감을 조용히 넘기지 않는다 — 어떻게 잡았는지 밝히고 새 마감을 묻는다 (#231)."""
    session = _RoutingSession(blocks=[], fixed=[], policies=[])
    config: Any = {"configurable": {"session": session, "tone_mode": None}}
    past = (TUE - timedelta(days=11)).isoformat()
    new_state = await first_plan.schedule_blocks(_freq_state(deadline=past), config)
    notice = next((w for w in new_state["schedule_warnings"] if past in w), None)
    assert notice is not None, "지난 마감 고지가 warnings 에 있어야 한다"
    assert "이미 지난 날짜" in notice
    assert "새로 정해주시면" in notice


async def test_future_deadline_gets_no_overdue_notice() -> None:
    """미래 마감엔 지난-마감 고지가 붙지 않는다 — 고지가 정상 계획을 오염시키면 안 된다."""
    session = _RoutingSession(blocks=[], fixed=[], policies=[])
    config: Any = {"configurable": {"session": session, "tone_mode": None}}
    new_state = await first_plan.schedule_blocks(_freq_state(), config)  # 마감 2026-08-01(미래)
    assert not [w for w in new_state["schedule_warnings"] if "이미 지난 날짜" in w]


def test_overdue_deadline_does_not_stretch_the_horizon_to_four_weeks() -> None:
    """지난 마감의 세션 분량은 1주치 — '마감 없음'(4주)과 같이 취급하면 안 된다.

    마감 없음은 *끝이 없다* 지만 지난 마감은 *늦었다* 라, 한 달치를 새로 벌이는 게 아니라
    따라잡을 만큼만 잡는다. 예전엔 `max(days, 0)` 덕에 우연히 1주였던 것을 의도로 못 박는다.
    """
    assert first_plan_adapter._horizon_weeks(TUE, (TUE - timedelta(days=11)).isoformat()) == 1
    assert first_plan_adapter._horizon_weeks(TUE, None) == first_plan_adapter._MAX_PLAN_WEEKS


def test_is_overdue_deadline_edges() -> None:
    """경계: 오늘은 지난 게 아니다 / 마감 없음·파싱 불가는 기존 경로에 맡긴다."""
    assert first_plan_adapter.is_overdue_deadline((TUE - timedelta(days=1)).isoformat(), TUE)
    assert not first_plan_adapter.is_overdue_deadline(TUE.isoformat(), TUE)
    assert not first_plan_adapter.is_overdue_deadline(None, TUE)
    assert not first_plan_adapter.is_overdue_deadline("언젠가", TUE)


# ── #190 하루 과부하 안내 ──────────────────────────────────────────────────


def _draft(day: date, hh: int, minutes: int) -> DraftScheduledBlock:
    start = datetime.combine(day, time(hh, 0), tzinfo=KST)
    return DraftScheduledBlock(
        interval=TimeInterval(start, start + timedelta(minutes=minutes)),
        origin="goal",
        origin_id=None,
        title="카드",
        category="study",
    )


def test_daily_overload_notice_counts_other_goals_too() -> None:
    """다른 목표의 확정분까지 합쳐서 상한 초과를 판단한다 — 사용자가 마주할 총량이 그것이다."""
    day = date(2026, 7, 30)
    notice = first_plan_adapter.daily_overload_notice(
        [_draft(day, 20, 60)],  # 이 계획은 60분뿐
        committed_min_by_day={day: 180},  # 다른 목표가 이미 180분
        cap_min=180,
    )
    assert notice is not None
    assert "7월 30일" in notice
    assert "4.0시간" in notice


def test_daily_overload_notice_silent_within_cap() -> None:
    """상한 안이면 아무 말도 하지 않는다 — 정상 계획에 잡음을 얹지 않는다."""
    day = date(2026, 7, 30)
    assert (
        first_plan_adapter.daily_overload_notice(
            [_draft(day, 20, 60)], committed_min_by_day={day: 120}, cap_min=180
        )
        is None
    )


def test_daily_overload_notice_names_one_day_and_counts_rest() -> None:
    """초과한 날이 여럿이면 가장 무거운 하루만 짚고 나머지는 개수로 — 날마다 늘어놓지 않는다."""
    d1, d2 = date(2026, 7, 30), date(2026, 7, 31)
    notice = first_plan_adapter.daily_overload_notice(
        [_draft(d1, 20, 240), _draft(d2, 20, 200)],
        committed_min_by_day={},
        cap_min=180,
    )
    assert notice is not None
    assert "7월 30일" in notice and "7월 31일" not in notice
    assert "2일" in notice


def test_daily_overload_notice_does_not_invent_a_deadline() -> None:
    """마감을 입력하지 않았으면 마감을 이유로 대지 않는다 — 없는 마감 지어내기 봉합.

    회귀(FE 실측): `goals.deadlines` 를 빈 값(마감 없음)으로 두고 계획을 만들었는데
    "**마감까지** 담으려면 이만큼이 필요해서예요" 가 그대로 나갔다. 사용자는 마감을 준 적이
    없다 — 모닝 브리프가 없는 어제를 지어내던 #224 와 같은 계열이다.
    """
    day = date(2026, 7, 30)
    kwargs: dict[str, Any] = {"committed_min_by_day": {day: 180}, "cap_min": 180}

    without = first_plan_adapter.daily_overload_notice([_draft(day, 20, 60)], **kwargs)
    assert without is not None
    assert "마감" not in without, f"마감 없는 계획인데 마감을 언급했다 — {without}"
    assert "이번 계획 분량을 담으려면" in without

    with_deadline = first_plan_adapter.daily_overload_notice(
        [_draft(day, 20, 60)], **kwargs, horizon="2026-09-30"
    )
    assert with_deadline is not None
    assert "마감(2026-09-30)까지 담으려면" in with_deadline  # 있으면 날짜까지 밝힌다


# ── 하루 상한이 세션 길이보다 작으면 안 된다 ──────────────────────────────


def test_daily_cap_never_below_one_session() -> None:
    """세션 하나가 상한을 넘으면 상한이 무의미해진다 — 상한을 세션 길이까지 올린다.

    회귀(FE 실측): `goals.session_length`="4시간 이상"(240분) + standard 상한(180분) 조합에서
    1차 배치의 상한 검사가 **이미 뭔가 잡혀 있는 모든 날**을 걸러내, 세션 대부분이 상한을
    무시하는 2차 패스로 넘어가며 사용자가 고른 '매일' 이 무너지고 하루 8시간짜리 날이 생겼다.
    """
    long_session = _outcome_with(session_length_min=240, frequency_per_week=7)
    assert first_plan_adapter.daily_cap_for_plan(long_session, "standard") == 240

    # 세션이 프리셋보다 짧으면 프리셋이 그대로 이긴다 (기존 동작 보존).
    short_session = _outcome_with(session_length_min=50, frequency_per_week=3)
    assert first_plan_adapter.daily_cap_for_plan(short_session, "standard") == 180
    assert first_plan_adapter.daily_cap_for_plan(short_session, "light") == 120


# ── 요청한 케이던스를 못 지켰으면 말한다 ──────────────────────────────────


def _outcome_with(*, session_length_min: int, frequency_per_week: int) -> InterviewOutcome:
    return InterviewOutcome(
        session_id="t-cad",
        generated_at=datetime.now(KST),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대3", season="방학"),
        core_goals=[
            GoalCandidate(
                title="개인 프로젝트 마무리",
                category="study",
                is_heaviest=True,
                tentative_tier="focus",
                confidence=0.9,
                session_length_min=session_length_min,
                frequency_per_week=frequency_per_week,
            )
        ],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:59"), peak_window=["심야"]
        ),
        preferences=PreferenceProfile(recovery_tone="따뜻", rest_ok=True, downscope_unit_min=30),
        horizon=None,
    )


def test_cadence_shortfall_named_with_reason() -> None:
    """'매일' 인데 절반의 날에만 잡혔으면 그 사실 + 이유(기존 계획)를 밝힌다."""
    start = date(2026, 8, 16)
    # 14일 구간인데 격일(7일)에만 배치 → 주 3.5회 < 7 * 0.8
    placed = [_draft(start + timedelta(days=i * 2), 9, 240) for i in range(7)]
    notice = first_plan_adapter.cadence_shortfall_notice(
        _outcome_with(session_length_min=240, frequency_per_week=7),
        placed,
        start_day=start,
        committed_min_by_day={start + timedelta(days=i): 189 for i in range(13)},
    )
    assert notice is not None
    assert "'매일'" in notice
    assert "13일 중 7일" in notice
    assert "이미 승인된 다른 계획이 그 기간 13일을 쓰고 있어서예요" in notice


def test_cadence_ok_is_silent() -> None:
    """요청한 케이던스를 지켰으면 아무 말도 하지 않는다 — 정상 계획에 잡음을 얹지 않는다."""
    start = date(2026, 8, 16)
    placed = [_draft(start + timedelta(days=i), 9, 240) for i in range(14)]  # 매일 14일
    assert (
        first_plan_adapter.cadence_shortfall_notice(
            _outcome_with(session_length_min=240, frequency_per_week=7),
            placed,
            start_day=start,
            committed_min_by_day={},
        )
        is None
    )


def test_cadence_notice_skipped_without_requested_frequency() -> None:
    """빈도를 안 고른 목표('몰아서')는 지킬 케이던스가 없으므로 침묵한다."""
    start = date(2026, 8, 16)
    outcome = _outcome_with(session_length_min=240, frequency_per_week=7)
    outcome.core_goals[0].frequency_per_week = None
    placed = [_draft(start, 9, 240)]
    assert (
        first_plan_adapter.cadence_shortfall_notice(
            outcome, placed, start_day=start, committed_min_by_day={}
        )
        is None
    )


def test_cadence_reason_falls_back_without_existing_plans() -> None:
    """기존 계획이 없으면 원인을 '자리가 없어서' 로 말한다 — 없는 계획을 탓하지 않는다."""
    start = date(2026, 8, 16)
    placed = [_draft(start + timedelta(days=i * 3), 9, 240) for i in range(4)]
    notice = first_plan_adapter.cadence_shortfall_notice(
        _outcome_with(session_length_min=240, frequency_per_week=7),
        placed,
        start_day=start,
        committed_min_by_day={},
    )
    assert notice is not None
    assert "다른 계획" not in notice
    assert "자리가 나오지 않아서예요" in notice


# ── 선호 시간대가 세션보다 짧을 때 ────────────────────────────────────────
#
# 회귀(FE 실측): 전역 집중 시간대를 '심야'(22:00~23:59 = 119분)로, 세션 길이를 '4시간 이상'
# (240분)으로 답한 사용자의 계획이 **전부 09:00** 에 잡혔다. 240 이 119 에 들어갈 리 없어
# 선호 창 탐색이 구조적으로 매번 실패하고 활동창 폴백으로 떨어진 것인데, **고지도 없었다**.


def _night_outcome(*, session_min: int, activity_end: str = "23:59") -> InterviewOutcome:
    return InterviewOutcome(
        session_id="t-night",
        generated_at=datetime.now(KST),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대3", season="방학"),
        core_goals=[
            GoalCandidate(
                title="개인 프로젝트",
                category="study",
                is_heaviest=True,
                tentative_tier="focus",
                confidence=0.9,
                session_length_min=session_min,
            )
        ],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end=activity_end), peak_window=["심야"]
        ),
        preferences=PreferenceProfile(recovery_tone="따뜻", rest_ok=True, downscope_unit_min=30),
        horizon=None,
    )


def test_preferred_window_missed_explains_session_longer_than_window() -> None:
    """창(2시간)보다 세션(4시간)이 길어 못 넣었으면 그 숫자로 이유를 말한다."""
    day = date(2026, 8, 16)
    placed = [_draft(day, 9, 240), _draft(day + timedelta(days=1), 9, 240)]  # 둘 다 09:00
    notice = first_plan_adapter.preferred_window_missed_notice(
        _night_outcome(session_min=240), placed
    )
    assert notice is not None
    assert "2개 중 0개만" in notice
    assert "한 번에 4시간" in notice
    assert "한 번에 하는 시간을 줄이거나" in notice
    # 119분을 정수 나눗셈해 "약 1시간" 이라고 말하던 버그 — 2시간에 가까우면 2시간이라고 한다.
    assert "약 2시간" in notice, f"창 길이 표기가 틀렸다 — {notice}"


def test_hours_label_does_not_truncate() -> None:
    """분 → 시간 표기가 잘리지 않는다 (119분을 '1시간' 이라 하지 않는다)."""
    assert first_plan_adapter._hours_label(240) == "4시간"
    assert first_plan_adapter._hours_label(90) == "1.5시간"
    assert first_plan_adapter._hours_label(119) == "2.0시간"


def test_preferred_window_respected_is_silent() -> None:
    """선호 시간대에 잡혔으면 아무 말도 하지 않는다."""
    day = date(2026, 8, 16)
    placed = [_draft(day, 22, 60), _draft(day + timedelta(days=1), 22, 60)]  # 22:00 = 심야
    assert (
        first_plan_adapter.preferred_window_missed_notice(_night_outcome(session_min=60), placed)
        is None
    )


def test_preferred_window_notice_skipped_without_preference() -> None:
    """시간대를 안 골랐으면 지킬 약속이 없으므로 침묵한다."""
    day = date(2026, 8, 16)
    outcome = _night_outcome(session_min=240)
    outcome.availability.peak_window = []
    assert first_plan_adapter.preferred_window_missed_notice(outcome, [_draft(day, 9, 240)]) is None


def test_earliest_fit_allows_starting_inside_window_and_running_over() -> None:
    """창보다 긴 세션도 **창 안에서 시작**하면 허용한다 — 창 밖으로 넘겨도 된다.

    심야(22:00~23:59)에 180분 세션: 예전엔 창 안에 다 못 들어가 09:00 폴백이었지만,
    활동창이 02:00 까지면 22:00 시작이 가능하다. 고른 시간대의 의도는 지켜진다.
    """
    day = date(2026, 8, 16)
    free = [TimeInterval(_at(day, 9), _at(day, 23) + timedelta(hours=3))]  # 09:00~02:00
    night = [TimeInterval(_at(day, 22), _at(day, 23) + timedelta(minutes=59))]
    start = plan_scheduler._earliest_fit(free, timedelta(minutes=180), night)
    assert start == _at(day, 22), f"심야 시작이어야 하는데 {start}"


def test_earliest_fit_prefers_full_containment_first() -> None:
    """창 안에 통째로 들어가는 자리가 있으면 그쪽이 이긴다 — 기존 우선순위 보존."""
    day = date(2026, 8, 16)
    free = [TimeInterval(_at(day, 9), _at(day, 23) + timedelta(minutes=59))]
    night = [TimeInterval(_at(day, 22), _at(day, 23) + timedelta(minutes=59))]
    start = plan_scheduler._earliest_fit(free, timedelta(minutes=60), night)
    assert start == _at(day, 22)


def test_earliest_fit_falls_back_when_window_start_has_no_room() -> None:
    """창 안에서 시작해도 자리가 안 나오면 기존대로 활동창 폴백."""
    day = date(2026, 8, 16)
    free = [TimeInterval(_at(day, 9), _at(day, 23) + timedelta(minutes=59))]
    night = [TimeInterval(_at(day, 22), _at(day, 23) + timedelta(minutes=59))]
    start = plan_scheduler._earliest_fit(free, timedelta(minutes=240), night)
    assert start == _at(day, 9), "창에 못 넣으면 활동창 가장 이른 지점"


# ── #252 자정을 넘는 활동창 ───────────────────────────────────────────────
#
# 회귀(실측): 활동 시간대 22:00~02:00 + 세션 3시간 → **블록 0개**, "배치할 가용 시간을 찾지
# 못했어요" 12줄. 배치가 달력 날짜 단위라 자정을 넘는 활동창이 [00:00~02:00] · [22:00~24:00]
# 두 조각으로 갈려 22:00→02:00 연속 4시간이 만들어지지 않았다 — 2시간 초과 세션은 100% 실패.
#
# 스케줄러가 자정에서 free 를 이어붙이도록 고쳐졌다(`plan_scheduler._join_midnight`).
# 이제 그 조합은 **정상 배치**되고, 안내는 "창 자체가 세션보다 짧을 때"만 나간다.


def _window_outcome(*, start: str, end: str, session_min: int) -> InterviewOutcome:
    return InterviewOutcome(
        session_id="t-win",
        generated_at=datetime.now(KST),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대3", season="방학"),
        core_goals=[
            GoalCandidate(
                title="사이드 프로젝트",
                category="study",
                is_heaviest=True,
                tentative_tier="focus",
                confidence=0.9,
                session_length_min=session_min,
            )
        ],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start=start, end=end), peak_window=["심야"]
        ),
        preferences=PreferenceProfile(recovery_tone="담백", rest_ok=True, downscope_unit_min=15),
        horizon=None,
    )


def test_narrow_window_notice_counts_the_joined_span_across_midnight() -> None:
    """자정을 넘는 창은 **이어붙인 길이**로 잰다 — 조각(2h)이 아니라 연속(4h) 기준.

    회귀: 조각 기준으로 재면 22:00~02:00 + 3시간이 '안 들어감' 으로 판정되는데, 스케줄러는
    이제 그걸 잘 배치한다 — 멀쩡한 계획에 거짓 경고가 붙는다.
    """
    assert (
        first_plan_adapter.narrow_activity_window_notice(
            _window_outcome(start="22:00", end="02:00", session_min=180)
        )
        is None
    ), "연속 4시간에 3시간 세션은 들어간다 — 경고하면 안 된다"


def test_narrow_window_notice_fires_when_even_joined_span_is_too_short() -> None:
    """이어붙여도 모자랄 때만 원인과 다음 행동을 숫자로 말한다 (22:00~02:00 = 4h < 5h)."""
    notice = first_plan_adapter.narrow_activity_window_notice(
        _window_outcome(start="22:00", end="02:00", session_min=300)
    )
    assert notice is not None
    assert "자정을 넘겨 이어지지만" in notice
    assert "약 4시간" in notice
    assert "한 번에 5시간씩" in notice
    assert "4시간 이하로 줄이거나" in notice


def test_narrow_window_notice_also_covers_plain_short_windows() -> None:
    """자정을 안 넘어도 창이 세션보다 짧으면 같은 증상 — 일반화된 안내가 나간다."""
    notice = first_plan_adapter.narrow_activity_window_notice(
        _window_outcome(start="09:00", end="11:00", session_min=180)
    )
    assert notice is not None
    assert "약 2시간" in notice
    assert "자정" not in notice, "자정을 안 넘는 창에 자정 문구가 붙으면 안 된다"


def test_narrow_window_silent_for_roomy_window() -> None:
    """세션이 들어가는 창은 침묵."""
    for start, end in (("09:00", "23:00"), ("09:00", "00:00")):
        assert (
            first_plan_adapter.narrow_activity_window_notice(
                _window_outcome(start=start, end=end, session_min=240)
            )
            is None
        ), f"{start}~{end}"


async def test_midnight_window_now_places_sessions_across_the_boundary() -> None:
    """#252 근본 수정 — 자정을 넘는 활동창에 조각보다 긴 세션이 실제로 배치된다."""
    session = _RoutingSession(blocks=[], fixed=[], policies=[])
    config: Any = {"configurable": {"session": session, "tone_mode": None}}
    outcome = _window_outcome(start="22:00", end="02:00", session_min=180)
    state = first_plan.initial_state(
        user_id=uuid4(), outcome=outcome, target_date=TUE.isoformat(), scope="horizon"
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
                estimated_minutes=180,
                category="study",
                first_step="시작",
            )
            for i in range(6)
        ],
        policy_violations=[],
    )
    new_state = await first_plan.schedule_blocks({**state, "goal_plan": gp}, config)
    warns = new_state["schedule_warnings"]
    blocks = new_state["scheduled_blocks"]

    # #252 핵심: 예전엔 여기가 0개였다. 이제 22:00→01:00 처럼 자정을 넘겨 배치된다.
    assert blocks, f"자정을 넘는 창에 3시간 세션이 배치돼야 한다 — warns={warns}"
    assert sum(1 for w in warns if "배치할 가용 시간을 찾지 못했어요" in w) == 0, (
        f"반복 실패 문구가 남았다 — {warns}"
    )
    crossing = [b for b in blocks if b.start.date() != b.end.date()]
    assert crossing, f"자정을 넘겨 이어진 블록이 하나도 없다 — {[(b.start, b.end) for b in blocks]}"
    for b in crossing:
        assert b.start.hour >= 22, f"활동창(22:00~02:00) 밖에서 시작했다 — {b.start}"
        assert b.end.hour <= 2, f"활동창 밖에서 끝났다 — {b.end}"


# ── #191 여백 덧대기 ───────────────────────────────────────────────────────


def test_pad_busy_adds_margin_on_both_sides() -> None:
    day = date(2026, 7, 30)
    iv = TimeInterval(
        datetime.combine(day, time(18, 0), tzinfo=KST),
        datetime.combine(day, time(19, 0), tzinfo=KST),
    )
    padded = pad_busy([BusyBlock(iv, "scheduled_block", "기존")], 20)
    assert padded[0].interval.start == datetime.combine(day, time(17, 40), tzinfo=KST)
    assert padded[0].interval.end == datetime.combine(day, time(19, 20), tzinfo=KST)


def test_pad_busy_clamps_to_the_same_day() -> None:
    """자정을 넘기지 않는다 — free 계산이 하루 단위라 앞뒤 날로 새면 안 된다."""
    day = date(2026, 7, 30)
    iv = TimeInterval(
        datetime.combine(day, time(23, 50), tzinfo=KST),
        datetime.combine(day, time(23, 59), tzinfo=KST),
    )
    padded = pad_busy([BusyBlock(iv, "scheduled_block", "기존")], 30)
    assert padded[0].interval.end == datetime.combine(day, time(0, 0), tzinfo=KST) + timedelta(
        days=1
    )


def test_pad_busy_noop_when_no_margin() -> None:
    day = date(2026, 7, 30)
    iv = TimeInterval(
        datetime.combine(day, time(18, 0), tzinfo=KST),
        datetime.combine(day, time(19, 0), tzinfo=KST),
    )
    blocks = [BusyBlock(iv, "scheduled_block", "기존")]
    assert pad_busy(blocks, 0) == blocks


def test_committed_minutes_by_day_sums_existing_blocks() -> None:
    day = date(2026, 7, 30)
    busy = {
        day: [
            BusyBlock(
                TimeInterval(
                    datetime.combine(day, time(18, 0), tzinfo=KST),
                    datetime.combine(day, time(19, 30), tzinfo=KST),
                ),
                "scheduled_block",
                "기존",
            ),
            BusyBlock(
                TimeInterval(
                    datetime.combine(day, time(21, 0), tzinfo=KST),
                    datetime.combine(day, time(22, 0), tzinfo=KST),
                ),
                "scheduled_block",
                "기존",
            ),
        ]
    }
    assert first_plan_adapter.committed_minutes_by_day(busy) == {day: 150}


# ── 캘린더 (ADR-0009 D4 — 다섯 번째 busy 소스) ─────────────────────────


async def test_schedule_blocks_avoids_calendar_events(monkeypatch: Any) -> None:
    """Google 캘린더 일정이 스케줄러까지 도달해 회피된다.

    이게 이 배선의 전부다 — freebusy 를 읽어도 `busy_for_day` 에 안 얹히면 계획은
    남의 일정 위에 그대로 잡힌다.
    """
    from reaction_backend.integrations.google_calendar import freebusy as fb

    async def _busy(session: Any, *, user_id: Any, start_day: date, end_day: date) -> Any:
        # 화·목 14:00~18:00 — 위 세 소스가 안 막는 시간대를 일부러 고른다.
        by_day: dict[date, list[BusyBlock]] = {}
        day = start_day
        while day <= end_day:
            if day.weekday() in (1, 3):
                by_day[day] = [
                    BusyBlock(TimeInterval(_at(day, 14), _at(day, 18)), "calendar", "캘린더 일정")
                ]
            day += timedelta(days=1)
        return by_day, "ok"

    monkeypatch.setattr(fb, "fetch_busy_by_day", _busy)

    session = _RoutingSession(blocks=[], fixed=[], policies=[])
    config: Any = {"configurable": {"session": session, "tone_mode": None}}

    new_state = await first_plan.schedule_blocks(_state(), config)
    blocks = new_state["scheduled_blocks"]
    assert blocks, "블록이 하나는 배치돼야 한다"

    for b in blocks:
        bs = b.start.astimezone(KST)
        be = b.end.astimezone(KST)
        if bs.weekday() in (1, 3):
            assert not _overlaps(bs, be, _at(bs.date(), 14), _at(bs.date(), 18)), (
                f"캘린더 일정 위에 배치됨: {bs}~{be}"
            )


def _state_many(count: int) -> Any:
    """카드를 많이 넣어 **2차 패스**(하루 상한 무시하고 남은 가용 시간 채우기)를 태운다."""
    state = _state()
    gp = state["goal_plan"]
    return {
        **state,
        "goal_plan": GoalDecomposition(
            goal_nodes=gp.goal_nodes,
            action_items=[
                ActionItemDraft(
                    node_id="n1",
                    title=f"작업{i}",
                    estimated_minutes=50,
                    category="study",
                    first_step="시작",
                )
                for i in range(count)
            ],
            policy_violations=[],
        ),
    }


async def test_calendar_is_avoided_by_the_second_pass_too(monkeypatch: Any) -> None:
    """1차뿐 아니라 **2차 패스**도 캘린더를 피해야 한다.

    1차는 `roomy_busy_for_day`, 2차는 `busy_for_day` 라는 **서로 다른 뷰**를 쓴다. 한쪽에만
    넣으면 하루가 빡빡해지는 순간(=2차가 도는 순간) 캘린더 일정 위에 카드가 잡힌다 —
    `pad_busy` 가 정확히 그 함정에 빠져 있다(ADR-0009 D4 ①). 카드를 잔뜩 넣어 1차를
    상한으로 막고 2차를 태운 상태에서 확인한다.
    """
    from reaction_backend.integrations.google_calendar import freebusy as fb

    async def _busy(session: Any, *, user_id: Any, start_day: date, end_day: date) -> Any:
        by_day: dict[date, list[BusyBlock]] = {}
        day = start_day
        while day <= end_day:
            by_day[day] = [
                BusyBlock(TimeInterval(_at(day, 14), _at(day, 18)), "calendar", "캘린더 일정")
            ]
            day += timedelta(days=1)
        return by_day, "ok"

    monkeypatch.setattr(fb, "fetch_busy_by_day", _busy)

    session = _RoutingSession(blocks=[], fixed=[], policies=[])
    config: Any = {"configurable": {"session": session, "tone_mode": None}}

    new_state = await first_plan.schedule_blocks(_state_many(12), config)
    blocks = new_state["scheduled_blocks"]
    assert len(blocks) >= 6, f"2차 패스를 태우기에 배치가 너무 적다: {len(blocks)}"

    for b in blocks:
        bs = b.start.astimezone(KST)
        be = b.end.astimezone(KST)
        assert not _overlaps(bs, be, _at(bs.date(), 14), _at(bs.date(), 18)), (
            f"캘린더 일정 위에 배치됨(2차 패스): {bs}~{be}"
        )


async def test_calendar_failure_does_not_break_planning(monkeypatch: Any) -> None:
    """캘린더를 못 읽어도 계획은 나온다 — 다만 연결한 사용자에게는 경고로 알린다."""
    from reaction_backend.integrations.google_calendar import freebusy as fb

    async def _failed(session: Any, *, user_id: Any, start_day: date, end_day: date) -> Any:
        return {}, "failed"

    monkeypatch.setattr(fb, "fetch_busy_by_day", _failed)

    session = _RoutingSession(blocks=[], fixed=[], policies=[])
    config: Any = {"configurable": {"session": session, "tone_mode": None}}

    new_state = await first_plan.schedule_blocks(_state(), config)

    assert new_state["scheduled_blocks"], "캘린더 실패가 계획을 죽였다"
    assert any("캘린더" in w for w in new_state["schedule_warnings"]), (
        "연결돼 있는데 못 읽었으면 사용자에게 알려야 한다"
    )


async def test_no_calendar_connection_is_silent(monkeypatch: Any) -> None:
    """연결 안 한 사용자(대다수)에게는 아무 말도 하지 않는다 — 매번 권유는 알림 피로다."""
    from reaction_backend.integrations.google_calendar import freebusy as fb

    async def _none(session: Any, *, user_id: Any, start_day: date, end_day: date) -> Any:
        return {}, "not_connected"

    monkeypatch.setattr(fb, "fetch_busy_by_day", _none)

    session = _RoutingSession(blocks=[], fixed=[], policies=[])
    config: Any = {"configurable": {"session": session, "tone_mode": None}}

    new_state = await first_plan.schedule_blocks(_state(), config)

    assert not any("캘린더" in w for w in new_state["schedule_warnings"])
