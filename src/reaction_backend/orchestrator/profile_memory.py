"""Profile 메모리 영속화 — InterviewOutcome 의 지속형 선호를 Policy Snapshot 레이어로.

인터뷰가 수집한 지속형(프로필/선호) 답을 `behavioral_profiles`·`interaction_styles`
(memory/README 의 "학습" 레이어)에 영속한다. 그동안 이 답들은 첫 계획에만 쓰이고
버려졌다(#A-1). 목표(goal-specific)는 여기 대상이 아니다 — `materialize_goals` 담당.

인터뷰 답은 한국어 칩 문자열이라("담백"·"오전"…) enum/버킷으로 정규화한다.
매핑 불가/누락 값은 채우지 않아 테이블 server_default 를 보존한다(안전).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.behavioral_profile import BehavioralProfile
from reaction_backend.db.models.interaction_style import InteractionStyle
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

# 역매핑 — 저장된 프로필(설정에서 수정 가능)을 재인터뷰 시드용 슬롯값으로 되돌린다(#reduce-reask).
# forward 가 다대일(유머·코치→encouraging)인 경우 대표 칩 1개로 되돌린다(약간 손실, 허용).
_CYCLE_TO_PEAK: dict[str, str] = {v: k for k, v in _PEAK_TO_CYCLE.items()}
_INTERACTION_TO_TONE: dict[str, str] = {"gentle": "따뜻", "normal": "담백", "encouraging": "유머"}


def seed_slots_from_profile(
    *,
    behavioral: BehavioralProfile | None,
    interaction: InteractionStyle | None,
    focus_mode_prefs: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """저장된 프로필 → 재인터뷰 시드 슬롯값(설정 수정이 반영된 '최신 진실').

    **설정에서 수정 가능한 필드만** 슬롯으로 되돌린다: 피크시간대·집중길이·회복톤·최소단위·
    휴식수용. 활동창(preferred_*)은 설정 편집 대상이 아니고 '24:00' 등이 프로필로 왕복되지
    않으므로 여기서 만들지 않는다(호출자가 지난 인터뷰 원답을 그대로 쓴다).
    """
    seed: dict[str, dict[str, Any]] = {}
    if behavioral is not None:
        peak = _CYCLE_TO_PEAK.get(behavioral.energy_cycle)
        if peak:
            seed["time.peak_window"] = {"type": "chip", "values": [peak]}
        if behavioral.attention_span:
            seed["energy.focus_duration"] = {
                "type": "chip",
                "values": [f"{behavioral.attention_span}분"],
            }
    if interaction is not None:
        tone = _INTERACTION_TO_TONE.get(interaction.recovery_tone)
        if tone:
            seed["recovery.tone"] = {"type": "chip", "values": [tone]}
    downscope = focus_mode_prefs.get("downscope_unit_min")
    if downscope is not None:
        seed["recovery.downscope_unit"] = {"type": "chip", "values": [f"{downscope}분"]}
    rest_ok = focus_mode_prefs.get("rest_ok")
    if rest_ok is not None:
        seed["recovery.rest_ok"] = {"type": "chip", "values": ["네" if rest_ok else "아니오"]}
    return seed


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
