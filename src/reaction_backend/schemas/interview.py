"""Interview 도메인 스키마 (api-contract §4) — S02 딥 인터뷰.

#3-B 단계는 정적 mock 스텁. 적응형 질문 선택·모호함 채점·LLM 호출은 #6.

#6 추가분:
- LLM Structured Output 스키마 (`NextQuestionSchema`, `AmbiguityUpdate`) —
  `aiClient.run(schema=...)` 로 강제 검증. 룰 fallback 도 같은 schema 로 반환.
- **경계 계약 `InterviewOutcome`** — Deep Interview(#6) 의 최종 산출물이자
  First Plan(#32) 의 유일한 입력 시드. slot_answers 의 결정적 투영으로 빌드(LLM 0회).
  자세한 흐름은 `orchestrator/interview.py` / `orchestrator/interview_adapter.py` 참조.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue

from reaction_backend.schemas.common import CamelModel, KstDatetime


class SlotCatalogEntry(CamelModel):
    """슬롯 카탈로그 한 항목 — GET /interview/slot-catalog."""

    slot_key: str
    label: str
    answer_type: str
    is_required: bool
    category: str


class Question(CamelModel):
    """인터뷰 질문 — 세션의 currentQuestion."""

    slot_key: str
    text: str
    answer_type: str
    options: list[str]


class InterviewSession(CamelModel):
    """인터뷰 세션 상태 — sessions·answers·next-question·finish 공통 응답."""

    session_id: str
    ambiguity_score: int
    total_turns: int
    end_reason: str | None
    current_question: Question | None


class SlotAnswerRequest(CamelModel):
    """POST /interview/sessions/{id}/answers 요청 — 슬롯 답 UPSERT."""

    slot_key: str = Field(min_length=1)
    value: JsonValue
    client_turn: int = Field(ge=0)


# ─────────────────────────────────────────────────────────────────────────────
# LLM Structured Output 스키마 (#6) — aiClient.run(schema=...) 강제 검증.
# 룰 fallback 도 동일 schema 인스턴스를 반환한다 (tool_executor 계약).
# ─────────────────────────────────────────────────────────────────────────────


class NextQuestionSchema(CamelModel):
    """LLM ① — `interview/next_question` 응답. 다음 질문 1개 + 직전 답 정규화/채점."""

    question: str
    clarity_score: float = Field(ge=0.0, le=1.0)
    normalized_value: str | None = None
    empathy_one_liner: str


class AmbiguityUpdate(CamelModel):
    """LLM ② — `interview/ambiguity_score` 응답. 슬롯 채점 결과 + 갱신된 모호함 지표."""

    slot_key: str
    clarity_score: float = Field(ge=0.0, le=1.0)
    new_ambiguity: float = Field(ge=0.0, le=1.0)


# ─────────────────────────────────────────────────────────────────────────────
# 경계 계약 — InterviewOutcome (Deep Interview #6 → First Plan #32)
#
# Interview 그래프 터미널에서 LLM 0회로 결정적 빌드 → S03 Analysis Confirm 화면에
# is_draft=true 로 노출 → 사용자 확정 후 First Plan 의 유일한 입력 시드.
# is_draft / ai_source 는 응답 시 라우터가 DraftMixin 으로 강제 (ADR-0005 §7.2).
# ─────────────────────────────────────────────────────────────────────────────

InterviewEndReason = Literal["completed", "turn_limit", "early_user", "abandoned"]


class TimeRange(CamelModel):
    """KST 로컬 시각 구간 (날짜 없음). 예: 09:00~23:00."""

    start: str = Field(description='"HH:MM" KST 로컬')
    end: str = Field(description='"HH:MM" KST 로컬')


class NoTouchWindow(CamelModel):
    """절대 일정 금지 구간 — time.no_touch. First Plan 의 no_touch 정책으로 전개."""

    days_of_week: list[str]  # WEEKDAY_KEYS 규약: ["mon","tue",...]
    window: TimeRange
    label: str | None = None


class AvailabilityProfile(CamelModel):
    """가용 시간 (time.* 슬롯군).

    First Plan 이 `time_policies` + `fixed_schedules` 로 전개해 free/busy 계산에 쓴다
    (`orchestrator/goal_structuring.py` 입력).
    """

    activity_window: TimeRange  # time.activity_window (필수)
    peak_window: list[str]  # time.peak_window chips (필수)
    no_touch_windows: list[NoTouchWindow] = Field(default_factory=list)  # time.no_touch
    fixed_block_hints: list[str] = Field(default_factory=list)  # time.fixed_blocks 자유입력 원문


class GoalCandidate(CamelModel):
    """핵심 목표 후보 (goals.* 슬롯군). First Plan 의 goal_node 분해 입력."""

    title: str  # goals.list 항목
    category: str  # study|health|career|... (자유 문자열, First Plan 이 정규화)
    is_heaviest: bool = False  # goals.heaviest
    deadline: str | None = None  # goals.deadlines "YYYY-MM-DD"
    why_now: str | None = None  # goals.why_now (선택)
    success_image: str | None = None  # goals.success_image
    tentative_tier: Literal["focus", "maintain", "parked"] = "maintain"
    confidence: float = Field(ge=0.0, le=1.0)  # 해당 슬롯 clarity_score


class PreferenceProfile(CamelModel):
    """선호 방식 (recovery.* + energy.* 슬롯군).

    First Plan 이 behavioral_profile / interaction_style 컨텍스트로 사용.
    """

    recovery_tone: str  # recovery.tone (필수)
    rest_ok: bool  # recovery.rest_ok (필수)
    downscope_ok: bool  # recovery.downscope_unit (필수)
    focus_duration_min: int | None = None  # energy.focus_duration (선택)
    break_pattern: str | None = None  # energy.break_pattern (선택)
    weekly_energy: str | None = None  # energy.weekly_drain (선택)


class IdentityContext(CamelModel):
    """정체성 (identity.* 슬롯군)."""

    role: str  # identity.role (필수)
    season: str  # identity.season (필수)
    major: str | None = None  # identity.major (선택)


class InterviewOutcome(CamelModel):
    """Deep Interview(#6) 의 최종 산출물이자 First Plan(#32) 의 유일한 시드.

    LLM 0회로 slot_answers 에서 결정적으로 빌드된다(`interview_adapter.build_outcome`).
    경계에서 추가 LLM 실패 표면을 만들지 않는다 (제약: 8s timeout / rate limit 안전).

    `schema_version` 은 경계 계약 버전 — #6/#32 가 독립 배포돼도 호환성을 검증할 수 있게
    명시한다. 깨지는 변경 시 bump.
    """

    session_id: str
    schema_version: Literal["1.0"] = "1.0"
    generated_at: KstDatetime  # now_kst() (시간 규칙: KST)
    end_reason: InterviewEndReason
    ambiguity_final: float = Field(ge=0.0, le=1.0)
    analysis_source: Literal["llm", "rule"] = "llm"  # 정규화가 룰 fallback 됐으면 "rule"

    identity: IdentityContext
    core_goals: list[GoalCandidate] = Field(min_length=1)  # 핵심 목표
    availability: AvailabilityProfile  # 가용 시간
    preferences: PreferenceProfile  # 선호 방식
    horizon: str | None = None  # 파생: max(core_goals.deadline) "YYYY-MM-DD"
    unresolved_slots: list[str] = Field(default_factory=list)  # default 처리된 필수 슬롯 키
