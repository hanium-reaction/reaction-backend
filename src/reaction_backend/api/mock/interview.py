"""Interview mock fixture — #3-B 스텁용 (S02 딥 인터뷰).

슬롯 카탈로그 · 데모 질문. 적응형 인터뷰 흐름·모호함 채점·LLM 호출은 #6 (실구현).

⚠️ 슬롯 카탈로그는 DevBaseline §6.2.2 "예시 v0.1" 기준. 원문 표는 20행이지만
요약은 "필수 11 + 선택 8 = 19"로 적혀 불일치 — 베타 전 PM 재검토 필요.
여기서는 표(20행)를 그대로 싣되 isRequired 는 그룹 라벨대로 둔다.
"""

from __future__ import annotations

from dataclasses import dataclass

# answer_type 값: chip | text | date_picker | time_range | select


@dataclass(frozen=True, slots=True)
class InterviewSlot:
    """슬롯 카탈로그 한 항목 (api-contract §4 — id·label·type·isRequired·category)."""

    slot_key: str
    label: str
    answer_type: str
    is_required: bool
    category: str


SLOT_CATALOG: tuple[InterviewSlot, ...] = (
    # [A] 정체성
    InterviewSlot("identity.role", "어떤 학년/시기예요?", "chip", True, "identity"),
    InterviewSlot("identity.season", "지금 학기 중이에요, 방학이에요?", "chip", True, "identity"),
    InterviewSlot("identity.major", "어떤 전공이에요?", "text", False, "identity"),
    # [B] 목표
    InterviewSlot(
        "goals.list", "지금 머릿속에 있는 일들을 편하게 알려주세요", "text", True, "goals"
    ),
    InterviewSlot(
        "goals.heaviest", "그중 가장 무겁게 느끼는 건 어떤 거예요?", "select", True, "goals"
    ),
    InterviewSlot("goals.deadlines", "마감일이 정해진 게 있어요?", "date_picker", True, "goals"),
    InterviewSlot(
        "goals.why_now", "그건 이번 학기에 꼭 끝내야 하는 이유가 있나요?", "text", False, "goals"
    ),
    InterviewSlot(
        "goals.success_image", "이번 주 끝에 어떤 모습이면 좋을까요?", "text", True, "goals"
    ),
    # [C] 시간
    InterviewSlot(
        "time.activity_window", "보통 몇 시부터 몇 시까지 활동해요?", "time_range", True, "time"
    ),
    InterviewSlot(
        "time.fixed_blocks", "매주 고정으로 비워야 하는 시간 있어요?", "text", True, "time"
    ),
    InterviewSlot("time.peak_window", "가장 잘 집중되는 시간대는요?", "chip", True, "time"),
    InterviewSlot("time.no_touch", "절대 일정 잡으면 안 되는 시간은요?", "chip", True, "time"),
    # [D] 패턴 & 에너지
    InterviewSlot(
        "energy.focus_duration", "한 번에 집중할 수 있는 시간은요?", "chip", False, "energy"
    ),
    InterviewSlot(
        "energy.break_pattern", "작업 사이 쉬는 시간은 어떻게 가져요?", "chip", False, "energy"
    ),
    InterviewSlot("energy.weekly_drain", "이번 주 컨디션은 어때요?", "chip", False, "energy"),
    # [E] 회복 선호
    InterviewSlot("recovery.tone", "못 한 날 어떤 톤이 좋아요?", "chip", True, "recovery"),
    InterviewSlot(
        "recovery.rest_ok", "쉬는 게 어때요 하는 제안을 받을 의향 있어요?", "chip", True, "recovery"
    ),
    InterviewSlot(
        "recovery.downscope_unit",
        "5분짜리로 줄어든 일도 의미 있게 느껴지나요?",
        "chip",
        True,
        "recovery",
    ),
    # [F] 외부 제약
    InterviewSlot(
        "constraints.special_events", "이번 달에 특별한 일정 있어요?", "text", False, "constraints"
    ),
    InterviewSlot(
        "constraints.current_burden",
        "지금 외부에서 받는 부담이 있나요?",
        "chip",
        False,
        "constraints",
    ),
)

# 필수 슬롯 수 — 스텁의 ambiguityScore 초기값으로 사용 (모호함 = 미해결 필수 슬롯 수).
REQUIRED_SLOT_COUNT = sum(1 for slot in SLOT_CATALOG if slot.is_required)


@dataclass(frozen=True, slots=True)
class DemoQuestion:
    """데모 인터뷰 질문 (currentQuestion 응답 형태)."""

    slot_key: str
    text: str
    answer_type: str
    options: tuple[str, ...]


# 데모 세션이 순서대로 내보내는 질문. 적응형 선택은 #6 — 여기선 고정 시퀀스.
DEMO_QUESTIONS: tuple[DemoQuestion, ...] = (
    DemoQuestion(
        "goals.list",
        "지금 머릿속에 있는 일들을 편하게 알려주세요. 예: 캡스톤, 토익, 운동",
        "text",
        (),
    ),
    DemoQuestion(
        "goals.heaviest",
        "그중 지금 가장 무겁게 느껴지는 건 어떤 거예요?",
        "select",
        ("캡스톤", "토익", "코딩테스트"),
    ),
    DemoQuestion(
        "time.peak_window",
        "하루 중 가장 잘 집중되는 시간대는 언제예요?",
        "chip",
        ("오전", "오후", "저녁", "심야", "변동"),
    ),
)

# 데모 인터뷰 세션 식별자 — 스텁은 이 id 만 유효한 세션으로 취급.
DEMO_SESSION_ID = "interview_demo_0001"
