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
    options: list[str] = Field(default_factory=list)  # chip/select 보기 (text 등은 빈 배열)


class Question(CamelModel):
    """인터뷰 질문 — 세션의 currentQuestion."""

    slot_key: str
    text: str
    answer_type: str
    options: list[str]


class InterviewSession(CamelModel):
    """인터뷰 세션 상태 — sessions·answers·next-question·finish 공통 응답.

    `ambiguity_score` 는 남은 미해결 필수 슬롯 수(정수). 진행될수록 감소 → 0 이면 충분.
    종료 턴(`end_reason` 채워지고 `current_question=None`)에는 `summary`(S03 확인 카드)와
    `outcome`(First Plan 시드)이 함께 실린다. 진행 중에는 둘 다 null.
    """

    session_id: str
    ambiguity_score: int
    total_turns: int
    end_reason: str | None
    current_question: Question | None
    summary: InterviewSummary | None = None
    outcome: InterviewOutcome | None = None


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
    """LLM ① — `interview/next_question` 응답. 다음 질문 1개 (+ 공감 한 줄).

    직전 답의 채점(clarity)·정규화(normalized_value)는 `AmbiguityUpdate`
    (`interview/ambiguity_score`) 가 전담한다 — 여기 두면 두 프롬프트가 같은 걸 중복
    계산하고 스키마가 드리프트한다. 라우터가 실제로 읽는 건 `question`.
    """

    question: str
    empathy_one_liner: str


class AmbiguityUpdate(CamelModel):
    """LLM ② — `interview/ambiguity_score` 응답. 슬롯 채점 + 모호함 + 구조화 정규화 값.

    `normalized_value` 는 자유서술 답을 슬롯 answer_type 에 맞는 구조로 추출한 값이다
    (딥 인터뷰는 채팅이라 답이 전부 자유서술 text 로 들어오는데, First Plan 시드
    `build_outcome` 은 chip/range/date 구조를 읽으므로 여기서 LLM 이 구조화해 저장한다):
    - chip/select   → 보기 중 하나(또는 배열)  예: "3학년" / ["오전","저녁"]
    - time_range    → {"start":"HH:MM","end":"HH:MM"}
    - date_picker   → "YYYY-MM-DD" (오늘 기준 상대표현 해석)
    - text          → 정리된 핵심값(문자열 또는 배열)
    추출 불가/무관한 답이면 null → 구조화 슬롯은 재질문, text 는 원문 저장으로 폴백.
    """

    slot_key: str
    clarity_score: float = Field(ge=0.0, le=1.0)
    new_ambiguity: float = Field(ge=0.0, le=1.0)
    normalized_value: JsonValue | None = None


class InterviewSummary(CamelModel):
    """LLM ③ — `interview/summary` 응답. Analysis Confirm(S03) 요약 확인 카드.

    필수 슬롯이 모두 채워진 뒤 `summarize_interview` 노드가 1회 생성한다.
    사람이 [이대로 진행/수정] 을 고르는 화면에 그대로 노출되는 표현 계층일 뿐,
    First Plan 의 입력 시드는 어디까지나 `InterviewOutcome` 이다(요약은 시드 아님).
    8s timeout / rate limit 시 슬롯에서 결정적으로 빌드한 룰 요약으로 fallback.
    """

    headline: str
    goal_summary: str
    time_summary: str
    preference_summary: str
    confirm_question: str


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


# InterviewSession 이 InterviewSummary/InterviewOutcome 보다 먼저 정의되므로
# (forward ref) 모두 정의된 뒤 재빌드해 응답 직렬화를 보장한다.
InterviewSession.model_rebuild()
