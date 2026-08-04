"""Interview State ↔ 경계 계약 어댑터 (ADR-0005 §7.4 규약).

`build_outcome` 은 Interview 그래프 터미널에서 호출되는 **순수 함수**다:
- slot_answers(인터뷰가 누적한 정규화 답) → `InterviewOutcome` 결정적 투영.
- **LLM 호출 0회 / DB 무관** → 경계에서 8s timeout·rate limit 실패 표면이 없다.
- early_finish/정체로 빈 필수 슬롯은 안전한 default 로 채우고 `unresolved_slots` 에 기록.

slot_answers 값 형식 (db/models/interview_slot_answer.py, value JSONB):
- chip:  {"type": "chip",  "values": ["오전", "저녁"]}
- text:  {"type": "text",  "raw": "...", "normalized": ["캡스톤", "토익"]}
- range: {"type": "range", "start": "09:00", "end": "23:00"}
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewEndReason,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)

# 필수 슬롯 중 비어 있으면 default 로 채우되 unresolved_slots 에 기록할 키.
# (mock.interview.SLOT_CATALOG 의 is_required=True 슬롯과 일치)
REQUIRED_SLOT_KEYS: tuple[str, ...] = (
    "identity.role",
    "identity.season",
    # 전역 집중 시간대는 목표를 묻기 **전** 프로필 단계에서 — goals.preferred_time 과 나란히
    # 두면 같은 질문을 두 번 하는 것처럼 읽힌다. 재인터뷰에선 이월(CARRY_OVER)돼 생략된다.
    "time.peak_window",
    "goals.list",
    "goals.heaviest",
    "goals.current_level",
    # 길이·빈도를 **먼저** 묻는다. 주당 시간 = 세션 길이 × 빈도라, 둘을 알면 세 번째는
    # 계산된다(`derived_weekly_hours`). 사람은 "한 번에 얼마나"(체감)·"주 며칠"(달력)은
    # 잘 답하지만 주 단위 총량은 곱셈이라 틀린다 — 실측에서 세 답이 다 있는 세션의 **절반**이
    # 1시간 이상 어긋났고(최대 6시간), 그 불일치가 매 계획의 분량 경고로 새어 나왔다.
    "goals.session_length",
    "goals.frequency",
    "goals.preferred_time",
    # 빈도를 '상관없음/몰아서'로 답해 계산이 안 될 때만 묻는다 (`is_slot_needed`).
    "goals.weekly_time",
    "goals.deadlines",
    "goals.success_image",
    "goals.approach",
    "goals.materials",
    "time.activity_window",
    "recovery.tone",
    "recovery.rest_ok",
    "recovery.downscope_unit",
)

# 재인터뷰 시 지난 완료 인터뷰에서 그대로 이어받는 '지속형(너에 대한)' 슬롯 — 매번 다시 묻지
# 않는다(#reduce-reask). 목표 관련(goals.*)과 이번 주 한정(energy.weekly_drain '이번 주 컨디션')은
# 계획 주기마다 바뀌므로 제외하고 새로 묻는다. 프로필은 설정에서 수정 가능하므로, 여기서 이어받아도
# 사용자가 언제든 바꿀 수 있다.
CARRY_OVER_SLOT_KEYS: frozenset[str] = frozenset(
    {
        "identity.role",
        "identity.season",
        "identity.major",
        "time.activity_window",
        "time.peak_window",
        "energy.focus_duration",
        "energy.break_pattern",
        "recovery.tone",
        "recovery.rest_ok",
        "recovery.downscope_unit",
    }
)

_DEFAULT_ACTIVITY = TimeRange(start="09:00", end="23:00")
_DEFAULT_TONE = "담백"
_DEFAULT_DOWNSCOPE_MIN = 10

# goals.list 미입력(early_finish 등) 시 core_goals min_length=1 계약을 맞추는 placeholder.
# 실제 Goal 로 영속하면 안 되며(#88), First Plan SAVING 이 이 sentinel 을 걸러낸다.
PLACEHOLDER_GOAL_TITLE = "(미입력 목표)"


def is_placeholder_goal(goal: GoalCandidate) -> bool:
    """미입력 placeholder 목표인지 — 영속/노출 대상에서 제외 (#88)."""
    return goal.confidence == 0.0 and goal.title == PLACEHOLDER_GOAL_TITLE


def is_filled_answer(value: Mapping[str, Any] | None) -> bool:
    """슬롯 값이 실질적으로 채워진 답인지.

    빈 값(None/빈 dict)과 재질문 대기 마커(`{"type":"pending"}`)는 미충족으로 본다
    (pending 은 시도 횟수만 누적하며 FSM 이 같은 슬롯을 다시 묻게 하는 임시 상태).
    스킵 마커(`{"type":"text","raw":""}`)는 '없음'을 명시한 유효 답이므로 충족으로 친다.
    """
    if not value:
        return False
    return value.get("type") != "pending"


# ─────────────────────────────────────────────────────────────────────────────
# 값 추출 헬퍼 (discriminated by value["type"])
# ─────────────────────────────────────────────────────────────────────────────


def _chip_values(value: Mapping[str, Any] | None) -> list[str]:
    if not value or value.get("type") != "chip":
        return []
    raw = value.get("values")
    return [str(v) for v in raw] if isinstance(raw, Sequence) and not isinstance(raw, str) else []


def _text_items(value: Mapping[str, Any] | None) -> list[str]:
    """text 슬롯의 normalized 리스트(없으면 raw 1개)."""
    if not value or value.get("type") != "text":
        return []
    norm = value.get("normalized")
    if isinstance(norm, Sequence) and not isinstance(norm, str):
        return [str(v) for v in norm if str(v).strip()]
    raw = value.get("raw")
    return [str(raw)] if isinstance(raw, str) and raw.strip() else []


def _text_raw(value: Mapping[str, Any] | None) -> str | None:
    if not value or value.get("type") != "text":
        return None
    raw = value.get("raw")
    return str(raw) if isinstance(raw, str) and raw.strip() else None


def _range(value: Mapping[str, Any] | None) -> TimeRange | None:
    if not value or value.get("type") != "range":
        return None
    start, end = value.get("start"), value.get("end")
    if isinstance(start, str) and isinstance(end, str):
        return TimeRange(start=start, end=end)
    return None


def _bool(value: Mapping[str, Any] | None, *, default: bool = False) -> bool:
    """chip 형태의 예/아니오 슬롯 해석. 첫 chip 값이 긍정 어휘면 True."""
    chips = _chip_values(value)
    if not chips:
        return default
    return chips[0] not in {"아니오", "no", "false", "싫어요", "거절"}


# ─────────────────────────────────────────────────────────────────────────────
# 경계 계약 빌드 (순수 함수)
# ─────────────────────────────────────────────────────────────────────────────


def build_outcome(
    *,
    session_id: str,
    slot_answers: Mapping[str, Mapping[str, Any] | None],
    ambiguity_final: float,
    end_reason: InterviewEndReason,
    analysis_source: Literal["llm", "rule"],
) -> InterviewOutcome:
    """slot_answers → InterviewOutcome. LLM 0회·순수함수.

    빈 필수 슬롯은 default 로 채우고 `unresolved_slots` 에 키를 남긴다 (First Plan 이
    VALIDATING 에서 보완 질문/재입력 분기를 띄울 수 있도록).
    """
    unresolved = [
        k
        for k in REQUIRED_SLOT_KEYS
        if is_slot_needed(k, slot_answers) and not is_filled_answer(slot_answers.get(k))
    ]

    identity = IdentityContext(
        role=_first(_chip_values(slot_answers.get("identity.role"))) or "미상",
        season=_first(_chip_values(slot_answers.get("identity.season"))) or "미상",
        major=_text_raw(slot_answers.get("identity.major")),
    )

    core_goals = _build_goals(slot_answers)
    availability = _build_availability(slot_answers)
    preferences = _build_preferences(slot_answers)
    horizon = _max_deadline(core_goals)

    return InterviewOutcome(
        session_id=session_id,
        generated_at=now_kst(),
        end_reason=end_reason,
        ambiguity_final=ambiguity_final,
        analysis_source=analysis_source,
        identity=identity,
        core_goals=core_goals,
        availability=availability,
        preferences=preferences,
        horizon=horizon,
        unresolved_slots=unresolved,
    )


def _first(items: Sequence[str]) -> str | None:
    return items[0] if items else None


def _build_goals(slot_answers: Mapping[str, Mapping[str, Any] | None]) -> list[GoalCandidate]:
    """goals.list 항목들을 GoalCandidate 로. heaviest 1개는 focus tier 로 표시.

    core_goals 는 min_length=1 계약이므로 비어 있으면 placeholder 1개를 둔다
    (unresolved_slots 에 'goals.list' 가 이미 기록되어 First Plan 이 보완 분기).
    """
    titles = _text_items(slot_answers.get("goals.list"))
    heaviest = _first(_chip_values(slot_answers.get("goals.heaviest"))) or _first(
        _text_items(slot_answers.get("goals.heaviest"))
    )
    deadline = _text_raw(slot_answers.get("goals.deadlines"))  # date_picker → raw "YYYY-MM-DD"
    success_image = _text_raw(slot_answers.get("goals.success_image"))
    why_now = _text_raw(slot_answers.get("goals.why_now"))
    current_level = _text_raw(slot_answers.get("goals.current_level"))
    # **직접 답한 값이 이긴다.** 새 인터뷰는 길이·빈도를 답하면 주당 시간을 아예 묻지 않으므로
    # (`is_slot_needed`) 자연히 유도값을 쓰고, 이미 저장된 세션은 종전 해석을 그대로 유지한다.
    #
    # 유도값을 우선하면 과거 데이터의 계획 분량이 말없이 바뀐다 — 실측에 '주 8시간 · 한 번
    # 2시간 · 매일' 세션이 있는데, 유도하면 14시간이 되어 **부하가 두 배**가 된다. 사용자가
    # 이미 승인한 계획의 전제를 뒤에서 바꾸는 셈이라 하위호환을 택한다.
    weekly_hours = _chip_hours(slot_answers.get("goals.weekly_time"))
    if weekly_hours is None:
        _derived = derived_weekly_hours(slot_answers)
        weekly_hours = max(1, round(_derived)) if _derived else None
    session_length = _chip_duration_min(slot_answers.get("goals.session_length"))
    preferred_time = _first(_chip_values(slot_answers.get("goals.preferred_time")))
    frequency = _chip_frequency(slot_answers.get("goals.frequency"))
    approach_note = _text_raw(slot_answers.get("goals.approach"))
    materials_note = _text_raw(slot_answers.get("goals.materials"))

    if not titles:
        return [
            GoalCandidate(
                title=PLACEHOLDER_GOAL_TITLE,
                category="other",
                tentative_tier="maintain",
                confidence=0.0,
            )
        ]

    goals: list[GoalCandidate] = []
    for title in titles:
        is_heaviest = heaviest is not None and title == heaviest
        goals.append(
            GoalCandidate(
                title=title,
                category="other",  # First Plan 이 카테고리 정규화 (베이스라인은 보류)
                is_heaviest=is_heaviest,
                deadline=deadline if is_heaviest else None,
                why_now=why_now if is_heaviest else None,
                success_image=success_image if is_heaviest else None,
                current_level=current_level if is_heaviest else None,
                weekly_hours=weekly_hours if is_heaviest else None,
                session_length_min=session_length if is_heaviest else None,
                preferred_time=preferred_time if is_heaviest else None,
                frequency_per_week=frequency if is_heaviest else None,
                approach_note=approach_note if is_heaviest else None,
                materials_note=materials_note if is_heaviest else None,
                tentative_tier="focus" if is_heaviest else "maintain",
                confidence=0.5,
            )
        )
    return goals


def _build_availability(
    slot_answers: Mapping[str, Mapping[str, Any] | None],
) -> AvailabilityProfile:
    activity = _range(slot_answers.get("time.activity_window")) or _DEFAULT_ACTIVITY
    peak = _chip_values(slot_answers.get("time.peak_window"))
    # no_touch_windows / fixed_block_hints 는 인터뷰에서 더 이상 채우지 않는다(#audit).
    # 둘 다 답을 받아도 **소비하는 코드가 없었다** — chip 카테고리만으로는 스케줄러가 쓸 실제
    # 시각이 없고(no_touch), 자유서술 힌트는 계획 코드 참조가 0회였다(fixed_blocks).
    # 실제 시각 차단은 활동창(수면 여집합) + 고정일정(S05, 요일·시각 보유)이 담당한다.
    # `fixed_block_hints` 필드 자체는 스키마에 남긴다 — 이미 저장된 outcome(JSON)이 이 키를
    # 갖고 있어, 필드를 지우면 옛 세션 역직렬화가 깨진다.
    return AvailabilityProfile(activity_window=activity, peak_window=peak)


def _build_preferences(
    slot_answers: Mapping[str, Mapping[str, Any] | None],
) -> PreferenceProfile:
    return PreferenceProfile(
        recovery_tone=_first(_chip_values(slot_answers.get("recovery.tone"))) or _DEFAULT_TONE,
        rest_ok=_bool(slot_answers.get("recovery.rest_ok"), default=True),
        downscope_unit_min=(
            _chip_minutes(slot_answers.get("recovery.downscope_unit")) or _DEFAULT_DOWNSCOPE_MIN
        ),
        focus_duration_min=_chip_minutes(slot_answers.get("energy.focus_duration")),
        break_pattern=_first(_chip_values(slot_answers.get("energy.break_pattern"))),
        weekly_energy=_first(_chip_values(slot_answers.get("energy.weekly_drain"))),
    )


def _chip_minutes(value: Mapping[str, Any] | None) -> int | None:
    """'25분'·'5분' 같은 chip 답에서 분(min) 정수를 추출. 숫자 없으면 None."""
    chips = _chip_values(value)
    if not chips:
        return None
    digits = "".join(c for c in chips[0] if c.isdigit())
    return int(digits) if digits else None


def derived_weekly_hours(slot_answers: Mapping[str, Any]) -> float | None:
    """세션 길이 × 빈도 → 주당 시간. 둘 중 하나라도 없으면 None.

    주당 시간을 **묻지 않고 계산하는** 이유: 세 값은 수학적으로 종속(총량 = 길이 × 빈도)인데
    셋 다 물으면 사용자가 곱셈을 대신해야 한다. 실측(세 답이 모두 있는 세션 8개) 중 4개가
    1시간 이상 어긋났고 최대 6시간 차이였다 — '주 8시간 · 한 번에 2시간 · 매일'(= 실제 14시간)
    같은 답이 거짓이 아니라, 머릿속에서 2×7 을 하지 않았을 뿐이다.
    """
    minutes = _chip_duration_min(slot_answers.get("goals.session_length"))
    freq = _chip_frequency(slot_answers.get("goals.frequency"))
    if not minutes or not freq:
        return None
    return round(minutes * freq / 60, 1)


def is_slot_needed(slot_key: str, slot_answers: Mapping[str, Any]) -> bool:
    """이 슬롯을 **지금 사용자에게 물어야 하는가**. 다른 답에서 유도되면 False.

    FSM(`interview._next_required_slot`)과 `unresolved_slots` 판정이 **같은 술어**를 써야
    한다 — 한쪽만 건너뛰면 묻지도 않은 슬롯이 영영 미해결로 남아 인터뷰가 끝나지 않는다.
    """
    if slot_key == "goals.weekly_time":
        return derived_weekly_hours(slot_answers) is None
    return True


def _chip_hours(value: Mapping[str, Any] | None) -> int | None:
    """'6시간'·'8시간 이상' 같은 chip 답에서 시간(hour) 정수를 추출. 숫자 없으면 None."""
    chips = _chip_values(value)
    if not chips:
        return None
    digits = "".join(c for c in chips[0] if c.isdigit())
    return int(digits) if digits else None


def _chip_duration_min(value: Mapping[str, Any] | None) -> int | None:
    """'1시간 30분'·'2시간'·'30분' 같은 chip 답에서 총 분(min)을 추출. 없으면 None.

    숫자 뒤 '시'(간)면 ×60, '분'이면 그대로 누적. 예: '1시간 30분' → 90, '2시간' → 120.
    """
    chips = _chip_values(value)
    if not chips:
        return None
    total, num = 0, ""
    for ch in chips[0]:
        if ch.isdigit():
            num += ch
        elif ch == "시" and num:  # '시간'
            total += int(num) * 60
            num = ""
        elif ch == "분" and num:
            total += int(num)
            num = ""
    return total or None


def _chip_frequency(value: Mapping[str, Any] | None) -> int | None:
    """'매일'·'주 3회' 같은 빈도 chip 에서 주당 횟수를 추출. '몰아서/상관없음'·미입력은 None.

    '매일'은 7로, '주 N회'는 N으로 환원한다. 숫자도 '매일'도 아니면(몰아서·상관없음) None →
    볼륨(weekly_hours) 기반 세션 수 산정으로 폴백한다.
    """
    chips = _chip_values(value)
    if not chips:
        return None
    first = chips[0]
    if "매일" in first:
        return 7
    digits = "".join(c for c in first if c.isdigit())
    return int(digits) if digits else None


def _max_deadline(goals: Sequence[GoalCandidate]) -> str | None:
    deadlines = [g.deadline for g in goals if g.deadline]
    return max(deadlines) if deadlines else None  # ISO "YYYY-MM-DD" 는 사전식 = 시간식
