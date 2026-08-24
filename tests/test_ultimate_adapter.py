"""`ultimate_adapter.build_ultimate_outcome` — 궁극목표 경계 계약 순수 함수 테스트 (PR2, #6-B).

`interview_adapter.build_outcome` 의 궁극목표판. LLM 호출 0회·DB 무관 — 표로 검증 가능.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from reaction_backend.db.models.goal import Goal
from reaction_backend.orchestrator import interview_catalog, ultimate_adapter
from reaction_backend.schemas.common import KST, now_kst
from reaction_backend.schemas.ultimate_goal import UltimateGoalOutcome
from tests.conftest import DEMO_USER_UUID, FakeInterviewRepo

FULL_SLOT_ANSWERS = {
    "ultimate.statement": {"type": "text", "raw": "메이저리그 8구단 드래프트 1순위"},
    "ultimate.domain": {"type": "chip", "values": ["체력·컨디션"]},
    "ultimate.horizon": {"type": "chip", "values": ["5년"]},
    "ultimate.measure": {"type": "text", "raw": "드래프트 1라운드 지명"},
    "ultimate.success_image": {"type": "text", "raw": "구단 유니폼을 입고 첫 공을 던지는 순간"},
    "ultimate.identity": {"type": "text", "raw": "매일 훈련을 거르지 않는 프로 지망생"},
    "ultimate.current_position": {"type": "text", "raw": "고교 3학년, 지역 대회 4강"},
    "ultimate.pillars_hint": {
        "type": "text",
        "raw": "구위, 멘탈",
        "normalized": ["구위", "멘탈"],
    },
    "ultimate.constraints": {
        "type": "text",
        "raw": "부상 이력, 체중 관리",
        "normalized": ["부상 이력", "체중 관리"],
    },
    "ultimate.values": {"type": "chip", "values": ["성장", "건강"]},
    "ultimate.assets": {"type": "text", "raw": "강한 어깨"},
    "ultimate.role_model": {"type": "text", "raw": "박찬호"},
}


def test_build_ultimate_outcome_projects_required_slots() -> None:
    outcome = ultimate_adapter.build_ultimate_outcome(
        session_id="iv_ultimate_1",
        slot_answers=FULL_SLOT_ANSWERS,
        ambiguity_final=0.05,
        end_reason="completed",
        analysis_source="llm",
    )
    assert outcome.statement == "메이저리그 8구단 드래프트 1순위"
    assert outcome.domain == "체력·컨디션"
    assert outcome.horizon_years == 5
    assert outcome.measure == "드래프트 1라운드 지명"
    assert outcome.success_image == "구단 유니폼을 입고 첫 공을 던지는 순간"
    assert outcome.identity_note == "매일 훈련을 거르지 않는 프로 지망생"
    assert outcome.current_position == "고교 3학년, 지역 대회 4강"
    assert outcome.pillars_hint == ["구위", "멘탈"]
    assert outcome.constraints == ["부상 이력", "체중 관리"]
    assert set(outcome.values) == {"성장", "건강"}
    assert outcome.assets == "강한 어깨"
    assert outcome.unresolved_slots == []
    assert outcome.analysis_source == "llm"


def test_build_ultimate_outcome_defaults_and_unresolved_when_empty() -> None:
    """early_finish/정체로 빈 슬롯 — 안전 default(빈 문자열/빈 리스트) + unresolved_slots 기록.

    `GoalCandidate` 와 달리 min_length=1 계약이 없어 placeholder sentinel 이 필요 없다
    (설계서 §5.4 근거 1) — 예외 없이 빈 필드로 채워진다는 것 자체가 이 테스트의 핵심.
    """
    outcome = ultimate_adapter.build_ultimate_outcome(
        session_id="iv_ultimate_2",
        slot_answers={},
        ambiguity_final=1.0,
        end_reason="early_user",
        analysis_source="rule",
    )
    assert outcome.statement == ""
    assert outcome.measure == ""
    assert outcome.constraints == []
    assert outcome.pillars_hint == []
    assert outcome.values == []
    assert outcome.assets is None
    assert outcome.horizon_years is None
    # 필수 9개가 전부 unresolved 로 기록된다.
    assert set(outcome.unresolved_slots) == set(interview_catalog.ULTIMATE_REQUIRED_SLOT_KEYS)
    assert outcome.analysis_source == "rule"


def test_build_ultimate_outcome_partial_unresolved() -> None:
    """일부만 답한 경우 — 답한 슬롯은 unresolved 에서 빠지고 나머지만 남는다."""
    partial = {
        "ultimate.statement": {"type": "text", "raw": "우주비행사가 되는 것"},
        "ultimate.domain": {"type": "chip", "values": ["역량"]},
    }
    outcome = ultimate_adapter.build_ultimate_outcome(
        session_id="iv_ultimate_3",
        slot_answers=partial,
        ambiguity_final=0.7,
        end_reason="early_user",
        analysis_source="rule",
    )
    assert "ultimate.statement" not in outcome.unresolved_slots
    assert "ultimate.domain" not in outcome.unresolved_slots
    assert "ultimate.measure" in outcome.unresolved_slots
    assert "ultimate.success_image" in outcome.unresolved_slots


def test_ultimate_outcome_serializes_camel_case() -> None:
    """envelope-less 도메인 객체 — camelCase 직렬화 + 역직렬화 round-trip."""
    outcome = ultimate_adapter.build_ultimate_outcome(
        session_id="iv_ultimate_4",
        slot_answers=FULL_SLOT_ANSWERS,
        ambiguity_final=0.05,
        end_reason="completed",
        analysis_source="llm",
    )
    dumped = outcome.model_dump(by_alias=True)
    assert "sessionId" in dumped
    assert "horizonYears" in dumped
    assert "currentPosition" in dumped
    assert "pillarsHint" in dumped
    assert "unresolvedSlots" in dumped
    json_str = outcome.model_dump_json(by_alias=True)
    assert "+09:00" in json_str  # generatedAt 은 KST
    restored = UltimateGoalOutcome.model_validate(dumped)
    assert restored.statement == outcome.statement


def test_horizon_years_maps_chip_to_int_or_none() -> None:
    """`ultimate.horizon` chip → 연 단위 정수. "기한 없음" 은 None(무기한)."""

    def horizon(chip: str) -> int | None:
        outcome = ultimate_adapter.build_ultimate_outcome(
            session_id="iv_h",
            slot_answers={"ultimate.horizon": {"type": "chip", "values": [chip]}},
            ambiguity_final=0.5,
            end_reason="early_user",
            analysis_source="rule",
        )
        return outcome.horizon_years

    assert horizon("3년") == 3
    assert horizon("5년") == 5
    assert horizon("7년") == 7
    assert horizon("10년") == 10
    assert horizon("10년 이상") == 10
    assert horizon("기한 없음") is None


def test_ultimate_carry_over_covers_every_ultimate_slot() -> None:
    """`ULTIMATE_CARRY_OVER_SLOT_KEYS` 는 ultimate.* 전량 이월 — 카탈로그의 모든 슬롯키와

    정확히 일치해야 한다(설계서 §2.6 "전량 이월"). 새 슬롯을 카탈로그에 추가하고 이 상수
    갱신을 잊으면 그 슬롯만 이월에서 조용히 빠진다 — 이 테스트가 드리프트를 잡는다.
    """
    catalog_keys = {s.slot_key for s in interview_catalog.ULTIMATE_CATALOG.slots}
    assert catalog_keys == ultimate_adapter.ULTIMATE_CARRY_OVER_SLOT_KEYS


def test_summary_variables_not_set_default() -> None:
    """빈 슬롯이면 각 값이 "아직 정하지 않음" — 지어내지 않는다는 계약."""
    v = ultimate_adapter.summary_variables({})
    assert v["statement"] == "아직 정하지 않음"
    assert v["measure"] == "아직 정하지 않음"


def test_summary_variables_joins_multiple_constraints() -> None:
    v = ultimate_adapter.summary_variables(FULL_SLOT_ANSWERS)
    assert v["constraints"] == "부상 이력, 체중 관리"
    assert v["statement"] == "메이저리그 8구단 드래프트 1순위"
    assert v["horizon"] == "5년"


# ───────────────────── resolve_outcome (PR5) ─────────────────────
# U1/U2/U3/U5 공용 — `routes/planning.py::_resolve_outcome`(계획 인터뷰)와 같은 패턴.


async def test_resolve_outcome_prefers_inline() -> None:
    """인라인 outcome 이 있으면 세션 조회 없이 그대로 돌려준다."""
    inline = ultimate_adapter.build_ultimate_outcome(
        session_id="iv_inline",
        slot_answers=FULL_SLOT_ANSWERS,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    repo = FakeInterviewRepo()  # 세션이 하나도 없어도 인라인이 있으면 조회 자체가 없다.
    result = await ultimate_adapter.resolve_outcome(repo, DEMO_USER_UUID, inline=inline)
    assert result is inline


async def test_resolve_outcome_recovers_from_latest_finished_session() -> None:
    """인라인이 없으면 최근 '정상 종료' kind=ultimate 세션에서 복구한다."""
    repo = FakeInterviewRepo()
    session_row = await repo.create_session(
        DEMO_USER_UUID, "gemini-3.5-flash-lite", kind="ultimate"
    )
    for slot_key, value in FULL_SLOT_ANSWERS.items():
        await repo.upsert_slot_answer(session_row.id, slot_key, value, is_required=True)
    await repo.finalize(session_row, end_reason="completed", total_turns=9, ambiguity_final=0.1)

    result = await ultimate_adapter.resolve_outcome(repo, DEMO_USER_UUID)
    assert result is not None
    assert result.statement == "메이저리그 8구단 드래프트 1순위"
    assert result.session_id == str(session_row.id)


async def test_resolve_outcome_ignores_plan_kind_session() -> None:
    """`kind='plan'` 세션은 아무리 최신이어도 무시한다 — kind 필터가 걸러야 한다."""
    repo = FakeInterviewRepo()
    plan_row = await repo.create_session(DEMO_USER_UUID, "gemini-3.5-flash-lite", kind="plan")
    await repo.finalize(plan_row, end_reason="completed", total_turns=3, ambiguity_final=0.1)

    result = await ultimate_adapter.resolve_outcome(repo, DEMO_USER_UUID)
    assert result is None


async def test_resolve_outcome_none_when_nothing_finished() -> None:
    repo = FakeInterviewRepo()
    result = await ultimate_adapter.resolve_outcome(repo, DEMO_USER_UUID)
    assert result is None


# ───────────────────── _deadline_from_horizon (ADR-0008 §2) ─────────────────


def test_deadline_from_horizon_adds_years_to_today() -> None:
    expected = now_kst().date().replace(year=now_kst().date().year + 5)
    assert ultimate_adapter._deadline_from_horizon(5) == expected


def test_deadline_from_horizon_none_when_no_horizon() -> None:
    """ "기한 없음" 은 horizon_years=None 으로 넘어오고, 그대로 마감 없음이다."""
    assert ultimate_adapter._deadline_from_horizon(None) is None


def test_deadline_from_horizon_handles_leap_day(monkeypatch: Any) -> None:
    """2/29 에 물어봤는데 N년 뒤가 평년이면 `date.replace` 가 ValueError — 2/28 로 보정."""
    leap_today = datetime.combine(date(2028, 2, 29), time(), tzinfo=KST)  # 2028 은 윤년
    monkeypatch.setattr(ultimate_adapter, "now_kst", lambda: leap_today)

    assert ultimate_adapter._deadline_from_horizon(3) == date(2031, 2, 28)  # 2031 은 평년


# ───────────────────── materialize_ultimate_goal (PR5) ─────────────────────


class _GoalResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _GoalSession:
    """`select(Goal)` 만 라우팅하는 fake session — `materialize_ultimate_goal` 전용."""

    def __init__(self, *, existing: Goal | None = None) -> None:
        self._existing = existing
        self.added: list[Any] = []

    async def execute(self, stmt: Any) -> _GoalResult:  # noqa: ARG002
        return _GoalResult([self._existing] if self._existing is not None else [])

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _outcome_with_statement(statement: str) -> UltimateGoalOutcome:
    slot_answers = {**FULL_SLOT_ANSWERS, "ultimate.statement": {"type": "text", "raw": statement}}
    return ultimate_adapter.build_ultimate_outcome(
        session_id="iv_materialize",
        slot_answers=slot_answers,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )


async def test_materialize_ultimate_goal_creates_new_row() -> None:
    session = _GoalSession()
    goal = await ultimate_adapter.materialize_ultimate_goal(
        session,  # type: ignore[arg-type]
        user_id=DEMO_USER_UUID,
        outcome=_outcome_with_statement("메이저리그 8구단 드래프트 1순위"),
    )
    assert goal.title == "메이저리그 8구단 드래프트 1순위"
    assert goal.status == "active"
    assert goal.goal_tier == "parked"
    assert goal.is_ultimate is True
    assert goal.category == "other"
    assert goal.priority_level == 3  # refresh 없이도 응답에 바로 실을 수 있어야 한다
    assert session.added == [goal]  # 신규 생성만 add() 됨
    # FULL_SLOT_ANSWERS 의 ultimate.horizon="5년"(ADR-0008 §2) — 프롬프트 문자열이 아니라
    # 실제 Goal.deadline 이 된다.
    assert goal.deadline == now_kst().date().replace(year=now_kst().date().year + 5)


async def test_materialize_ultimate_goal_no_deadline_when_horizon_unbounded() -> None:
    """ "기한 없음" 이면 Goal.deadline 도 그대로 None."""
    slot_answers = {
        **FULL_SLOT_ANSWERS,
        "ultimate.horizon": {"type": "chip", "values": ["기한 없음"]},
    }
    outcome = ultimate_adapter.build_ultimate_outcome(
        session_id="iv_no_horizon",
        slot_answers=slot_answers,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    session = _GoalSession()

    goal = await ultimate_adapter.materialize_ultimate_goal(
        session,  # type: ignore[arg-type]
        user_id=DEMO_USER_UUID,
        outcome=outcome,
    )

    assert goal.deadline is None


async def test_materialize_ultimate_goal_reuses_existing_row() -> None:
    """이미 있으면 같은 행을 갱신 — 신규 생성 X(사용자당 1개, `Goal.is_ultimate`)."""
    existing = Goal()
    existing.id = UUID("55555555-5555-4555-8555-555555555555")
    existing.user_id = DEMO_USER_UUID
    existing.title = "옛 선언문"
    existing.is_ultimate = True
    existing.status = "active"
    existing.goal_tier = "parked"
    existing.deadline = date(2020, 1, 1)  # 재인터뷰가 이 값을 갈아치우는지 확인용
    existing.archived_at = None
    session = _GoalSession(existing=existing)

    goal = await ultimate_adapter.materialize_ultimate_goal(
        session,  # type: ignore[arg-type]
        user_id=DEMO_USER_UUID,
        outcome=_outcome_with_statement("새 선언문"),
    )
    assert goal is existing
    assert goal.title == "새 선언문"  # 갱신됨
    # horizon 이 바뀌었을 수 있으니 재인터뷰 때마다 deadline 도 다시 계산한다(옛 값 안 남음).
    assert goal.deadline == now_kst().date().replace(year=now_kst().date().year + 5)
    assert session.added == []  # 재사용(신규 add X)
