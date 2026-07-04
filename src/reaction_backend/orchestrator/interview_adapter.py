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
    NoTouchWindow,
    PreferenceProfile,
    TimeRange,
)

# 필수 슬롯 중 비어 있으면 default 로 채우되 unresolved_slots 에 기록할 키.
# (mock.interview.SLOT_CATALOG 의 is_required=True 슬롯과 일치)
REQUIRED_SLOT_KEYS: tuple[str, ...] = (
    "identity.role",
    "identity.season",
    "goals.list",
    "goals.heaviest",
    "goals.deadlines",
    "goals.success_image",
    "time.activity_window",
    "time.fixed_blocks",
    "time.peak_window",
    "time.no_touch",
    "recovery.tone",
    "recovery.rest_ok",
    "recovery.downscope_unit",
)

_DEFAULT_ACTIVITY = TimeRange(start="09:00", end="23:00")
_DEFAULT_TONE = "담백"

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
    unresolved = [k for k in REQUIRED_SLOT_KEYS if not is_filled_answer(slot_answers.get(k))]

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
    no_touch_chips = _chip_values(slot_answers.get("time.no_touch"))
    no_touch_windows = (
        [NoTouchWindow(days_of_week=[], window=activity, label=", ".join(no_touch_chips))]
        if no_touch_chips
        else []
    )
    fixed_hints = _text_items(slot_answers.get("time.fixed_blocks"))
    return AvailabilityProfile(
        activity_window=activity,
        peak_window=peak,
        no_touch_windows=no_touch_windows,
        fixed_block_hints=fixed_hints,
    )


def _build_preferences(
    slot_answers: Mapping[str, Mapping[str, Any] | None],
) -> PreferenceProfile:
    return PreferenceProfile(
        recovery_tone=_first(_chip_values(slot_answers.get("recovery.tone"))) or _DEFAULT_TONE,
        rest_ok=_bool(slot_answers.get("recovery.rest_ok"), default=True),
        downscope_ok=_bool(slot_answers.get("recovery.downscope_unit"), default=True),
        focus_duration_min=_focus_minutes(slot_answers.get("energy.focus_duration")),
        break_pattern=_first(_chip_values(slot_answers.get("energy.break_pattern"))),
        weekly_energy=_first(_chip_values(slot_answers.get("energy.weekly_drain"))),
    )


def _focus_minutes(value: Mapping[str, Any] | None) -> int | None:
    chips = _chip_values(value)
    if not chips:
        return None
    digits = "".join(c for c in chips[0] if c.isdigit())
    return int(digits) if digits else None


def _max_deadline(goals: Sequence[GoalCandidate]) -> str | None:
    deadlines = [g.deadline for g in goals if g.deadline]
    return max(deadlines) if deadlines else None  # ISO "YYYY-MM-DD" 는 사전식 = 시간식
