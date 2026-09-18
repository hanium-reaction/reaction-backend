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
from reaction_backend.orchestrator.interview_catalog import (
    PLAN_CATALOG,
    canonical_chip_values,
)
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
# ⚠️ 키는 **카탈로그 보기 문자열 그대로**여야 한다 — 보기는 "코치처럼" 인데 키가 "코치" 뿐이라
# 그 칩을 고른 사용자가 전부 'normal' 로 저장되고 있었다. "코치" 는 예전 표기로 남겨 둔다.
_TONE_TO_INTERACTION: dict[str, str] = {
    "따뜻": "gentle",
    "담백": "normal",
    "유머": "encouraging",
    "코치처럼": "encouraging",
    "코치": "encouraging",
}

# recovery.tone 칩 → users.tone_mode(gentle/strict/encouraging) — AI 말투 prefix 가 읽는 값
# (`llm.prompt_compose`). 설정 화면의 '코칭 톤' 이 이 값이다. '담백' 은 prefix 없는 기본
# 말투가 곧 담백이라 매핑하지 않는다(None 유지 → 설정에서 아무것도 안 고른 상태).
_TONE_TO_USER_TONE_MODE: dict[str, str] = {
    "따뜻": "gentle",
    "유머": "encouraging",
    "코치처럼": "encouraging",
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

    def _chip(slot_key: str, raw: str) -> None:
        """카탈로그 옵션으로 정규화해서만 시드에 넣는다 — 못 맞추면 **아예 안 넣는다**.

        시드는 `routes/interview._persist_turn` 을 타고 `interview_slot_answers` 에 UPSERT
        되므로, 옵션에 없는 표기를 넣으면 **사용자가 고른 적 없는 값이 사용자의 답으로**
        남는다. 실제로 그랬다: 프로필의 `attention_span` 을 `f"{n}분"` 으로 되돌리는데 그
        표기가 옵션에 없어(`"120분"` vs `"2시간 이상"`) 그대로 저장됐고, 오염된 프로필에서는
        `"2분"` 이 사용자의 답인 것처럼 남아 백필을 틀리게 할 뻔했다(v2.01).

        안 넣으면 그 슬롯은 열린 채 남아 인터뷰가 실제 보기를 들고 묻는다 — 지어낸 답으로
        슬롯을 닫는 것보다 낫다.
        """
        values = canonical_chip_values(PLAN_CATALOG.by_key.get(slot_key), [raw])
        if values:
            seed[slot_key] = {"type": "chip", "values": values}

    if behavioral is not None:
        peak = _CYCLE_TO_PEAK.get(behavioral.energy_cycle)
        if peak:
            _chip("time.peak_window", peak)
        if behavioral.attention_span:
            _chip("energy.focus_duration", f"{behavioral.attention_span}분")
    if interaction is not None:
        tone = _INTERACTION_TO_TONE.get(interaction.recovery_tone)
        if tone:
            _chip("recovery.tone", tone)
    downscope = focus_mode_prefs.get("downscope_unit_min")
    if downscope is not None:
        _chip("recovery.downscope_unit", f"{downscope}분")
    rest_ok = focus_mode_prefs.get("rest_ok")
    if rest_ok is not None:
        _chip("recovery.rest_ok", "네" if rest_ok else "아니오")
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


def user_tone_mode_from_chip(raw: str) -> str | None:
    """회복 톤 칩 → users.tone_mode. '담백'·미지원이면 None(기본 말투)."""
    return _TONE_TO_USER_TONE_MODE.get(raw)


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

    # 인터뷰에서 고른 톤 → AI 말투(users.tone_mode). 예전엔 이 답이 interaction_styles 에만
    # 저장되고 실제 말투를 정하는 tone_mode 는 설정 화면에서만 바뀌어, "따뜻하게" 를 골라도
    # 설정의 코칭 톤은 빈 칸이고 AI 말투도 기본값이었다. **비어 있을 때만** 채운다 — 사용자가
    # 설정에서 직접 고른 톤을 재인터뷰가 덮어쓰면 안 된다.
    if user.tone_mode is None:
        seeded = user_tone_mode_from_chip(prefs.recovery_tone)
        if seeded is not None:
            user.tone_mode = seeded
