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
    # chip/select 보기. text/date_picker/time_range 는 (). goals.heaviest 는 런타임 동적 생성.
    options: tuple[str, ...] = ()


SLOT_CATALOG: tuple[InterviewSlot, ...] = (
    # [A] 정체성
    InterviewSlot(
        "identity.role",
        "어떤 학년/시기예요?",
        "chip",
        True,
        "identity",
        options=("1학년", "2학년", "3학년", "4학년", "졸업유예", "대학원", "기타"),
    ),
    InterviewSlot(
        "identity.season",
        "지금 학기 중이에요, 방학이에요?",
        "chip",
        True,
        "identity",
        options=("학기 중", "방학", "계절학기"),
    ),
    InterviewSlot("identity.major", "어떤 전공이에요?", "text", False, "identity"),
    # [B] 목표
    InterviewSlot(
        "goals.list", "지금 머릿속에 있는 일들을 편하게 알려주세요", "text", True, "goals"
    ),
    # goals.heaviest 보기는 goals.list 응답에서 런타임 동적 생성 (라우터 _question_options).
    InterviewSlot(
        "goals.heaviest", "그중 가장 무겁게 느끼는 건 어떤 거예요?", "select", True, "goals"
    ),
    InterviewSlot(
        "goals.current_level",
        "그 목표, 지금 어느 정도까지 해봤어요? (처음이면 '처음이에요' 라고 알려주세요)",
        "text",
        True,
        "goals",
    ),
    # 목표별 주당 가용 시간 — 분해가 '얼마나 만들지'를 사용자의 실제 시간에 맞춰 산정한다.
    InterviewSlot(
        "goals.weekly_time",
        "이 목표에 일주일에 몇 시간 정도 쓸 수 있어요?",
        "chip",
        True,
        "goals",
        options=("2시간", "4시간", "6시간", "8시간 이상"),
    ),
    # 목표별 한 번에 집중/수행 가능한 시간 — 세션 길이·개수를 목표마다 다르게 잡는다.
    InterviewSlot(
        "goals.session_length",
        "이 목표는 한 번에 어느 정도 집중해서 할 수 있어요?",
        "chip",
        True,
        "goals",
        options=("30분", "1시간", "1시간 30분", "2시간"),
    ),
    # 목표별 선호 시간대 — 스케줄러가 이 목표를 배치할 때 전역 peak 대신 이 시간대를 우선한다.
    InterviewSlot(
        "goals.preferred_time",
        "이 목표는 주로 언제 하고 싶어요?",
        "chip",
        True,
        "goals",
        options=("오전", "오후", "저녁", "심야", "상관없음"),
    ),
    InterviewSlot("goals.deadlines", "마감일이 정해진 게 있어요?", "date_picker", True, "goals"),
    InterviewSlot(
        "goals.why_now", "그건 이번 학기에 꼭 끝내야 하는 이유가 있나요?", "text", False, "goals"
    ),
    InterviewSlot(
        "goals.success_image", "이번 주 끝에 어떤 모습이면 좋을까요?", "text", True, "goals"
    ),
    # 목표 접근 — 사용자가 선호하는 방식·순서로 분해를 grounding (없으면 넘겨도 됨).
    InterviewSlot(
        "goals.approach",
        "이 목표, 어떻게 해나가고 싶어요? 선호하는 방식·순서가 있으면 알려주세요 (없으면 넘겨도 돼요)",
        "text",
        True,
        "goals",
    ),
    # 목표 참고 자료 원문 — pointer 가 아니라 실제 내용을 붙여넣어야 분해가 그대로 뼈대로 쓴다.
    InterviewSlot(
        "goals.materials",
        "참고할 자료가 있으면 그 내용을 그대로 붙여넣어 주세요 — 프로젝트 설명·README·강의계획서·요구사항 등 (없으면 넘겨도 돼요)",
        "text",
        True,
        "goals",
    ),
    # [C] 시간
    InterviewSlot(
        "time.activity_window",
        "하루 중 계획을 잡아도 되는 시간대는 몇 시부터 몇 시까지예요? (이 시간 밖엔 일정을 안 잡아요)",
        "time_range",
        True,
        "time",
    ),
    InterviewSlot(
        # 필수 아님(#audit): 답이 fixed_block_hints 로만 남고 스케줄러가 소비하지 않아 계획에
        # 영향이 없다. 실제 고정 일정은 별도 fixed_schedules(S05)로 받으므로 인터뷰 필수에서 제외.
        "time.fixed_blocks",
        "매주 고정으로 비워야 하는 시간 있어요?",
        "text",
        False,
        "time",
    ),
    InterviewSlot(
        "time.peak_window",
        "가장 잘 집중되는 시간대는요?",
        "chip",
        True,
        "time",
        options=("오전", "오후", "저녁", "심야", "변동"),
    ),
    InterviewSlot(
        "time.no_touch",
        "절대 일정 잡으면 안 되는 시간은요?",
        "chip",
        True,
        "time",
        options=("수면", "식사", "통학·이동", "아르바이트", "가족 시간", "없음"),
    ),
    # [D] 패턴 & 에너지
    InterviewSlot(
        "energy.focus_duration",
        "한 번에 집중할 수 있는 시간은요?",
        "chip",
        False,
        "energy",
        options=("25분", "50분", "90분", "2시간 이상"),
    ),
    InterviewSlot(
        "energy.break_pattern",
        "작업 사이 쉬는 시간은 어떻게 가져요?",
        "chip",
        False,
        "energy",
        options=("짧게 자주", "길게 가끔", "거의 안 쉼"),
    ),
    InterviewSlot(
        "energy.weekly_drain",
        "이번 주 컨디션은 어때요?",
        "chip",
        False,
        "energy",
        options=("좋음", "보통", "지친 편", "많이 지침"),
    ),
    # [E] 회복 선호
    InterviewSlot(
        "recovery.tone",
        "못 한 날 어떤 톤이 좋아요?",
        "chip",
        True,
        "recovery",
        options=("담백", "따뜻", "유머", "코치처럼"),
    ),
    InterviewSlot(
        "recovery.rest_ok",
        "쉬는 게 어때요 하는 제안을 받을 의향 있어요?",
        "chip",
        True,
        "recovery",
        options=("네", "아니오"),
    ),
    InterviewSlot(
        "recovery.downscope_unit",
        "계획이 밀렸을 때, 할 일을 몇 분짜리까지 줄이면 그래도 해볼 만할까요?",
        "chip",
        True,
        "recovery",
        options=("5분", "10분", "15분", "30분"),
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
        options=("없음", "학업", "대인관계", "건강", "경제", "기타"),
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
