"""Profile 메모리 영속화 — InterviewOutcome 의 지속형 선호를 Policy Snapshot 레이어로.

인터뷰가 수집한 지속형(프로필/선호) 답을 `behavioral_profiles`·`interaction_styles`
(memory/README 의 "학습" 레이어)에 영속한다. 그동안 이 답들은 첫 계획에만 쓰이고
버려졌다(#A-1). 목표(goal-specific)는 여기 대상이 아니다 — `materialize_goals` 담당.

인터뷰 답은 한국어 칩 문자열이라("담백"·"오전"…) enum/버킷으로 정규화한다.
매핑 불가/누락 값은 채우지 않아 테이블 server_default 를 보존한다(안전).
"""

from __future__ import annotations

from datetime import time

from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.user import User
from reaction_backend.repositories.profile_repo import ProfileRepo
from reaction_backend.schemas.interview import InterviewOutcome

# time.peak_window 칩 → behavioral_profiles.energy_cycle enum.
_PEAK_TO_CYCLE: dict[str, str] = {
    "오전": "morning",
    "오후": "afternoon",
    "저녁": "evening",
    "심야": "night",
    "변동": "varies",
}

# recovery.tone 칩 → interaction_styles.recovery_tone enum(gentle/normal/encouraging).
_TONE_TO_INTERACTION: dict[str, str] = {
    "따뜻": "gentle",
    "담백": "normal",
    "유머": "encouraging",
    "코치": "encouraging",
}

# behavioral_profiles.time_chunk_preference 는 "10/20/30/60/90" 버킷(VARCHAR).
_CHUNK_BUCKETS: tuple[int, ...] = (10, 20, 30, 60, 90)


def energy_cycle_from_peak(peak_window: list[str]) -> str:
    """피크시간 칩(첫 값) → energy_cycle. 비었거나 미지원이면 'varies'."""
    first = peak_window[0] if peak_window else ""
    return _PEAK_TO_CYCLE.get(first, "varies")


def chunk_bucket(focus_minutes: int | None) -> str:
    """집중 지속(분) → 가장 가까운 블록 버킷 문자열. 없으면 '30'."""
    if not focus_minutes:
        return "30"
    nearest = min(_CHUNK_BUCKETS, key=lambda b: abs(b - focus_minutes))
    return str(nearest)


def recovery_tone_enum(raw: str) -> str:
    """회복 톤 칩 → interaction recovery_tone enum. 미지원이면 'normal'."""
    return _TONE_TO_INTERACTION.get(raw, "normal")


def _parse_hhmm(value: str) -> time | None:
    """'HH:MM' → time. 파싱 실패면 None(해당 필드 미기록 → default 보존)."""
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return None


def recovery_speed_from_prefs(downscope_unit_min: int | None, rest_ok: bool) -> str:
    """회복 최소 단위 + 휴식 수용 → behavioral_profiles.recovery_speed_type(fast/medium/slow).

    작은 단위로도 재시작 가능(≤10분)하고 휴식을 받아들이면 회복이 빠른 편, 큰 단위(≥30분)만
    가능하면 느린 편으로 파생한다(스펙 §5.25 — 회복 카드 페이싱에 쓰임).
    """
    if downscope_unit_min is not None and downscope_unit_min <= 10 and rest_ok:
        return "fast"
    if downscope_unit_min is not None and downscope_unit_min >= 30:
        return "slow"
    return "medium"


async def persist_profile_from_outcome(
    session: AsyncSession, *, user: User, outcome: InterviewOutcome
) -> None:
    """인터뷰 outcome 의 지속형 선호를 프로필 메모리에 영속.

    - behavioral_profile: 에너지/집중/시간 + recovery_speed_type(파생)
    - interaction_style: recovery_tone
    - users.focus_mode_preferences(JSONB): 회복 최소 단위·휴식 수용 (전용 컬럼이 없어 여기 저장)

    commit 은 호출자(인터뷰 finalize) 책임 — materialize_goals 와 같은 트랜잭션.
    """
    repo = ProfileRepo(session)
    availability = outcome.availability
    prefs = outcome.preferences

    await repo.upsert_behavioral(
        user.id,
        fields={
            "energy_cycle": energy_cycle_from_peak(availability.peak_window),
            "attention_span": prefs.focus_duration_min or 30,
            "time_chunk_preference": chunk_bucket(prefs.focus_duration_min),
            "preferred_start_time": _parse_hhmm(availability.activity_window.start),
            "preferred_end_time": _parse_hhmm(availability.activity_window.end),
            "recovery_speed_type": recovery_speed_from_prefs(
                prefs.downscope_unit_min, prefs.rest_ok
            ),
        },
    )
    await repo.upsert_interaction(
        user.id,
        fields={"recovery_tone": recovery_tone_enum(prefs.recovery_tone)},
    )

    # 회복 최소 단위·휴식 수용 → users.focus_mode_preferences (마이그레이션 불필요, 새 dict 재대입).
    fmp = dict(user.focus_mode_preferences or {})
    fmp["downscope_unit_min"] = prefs.downscope_unit_min
    fmp["rest_ok"] = prefs.rest_ok
    user.focus_mode_preferences = fmp
