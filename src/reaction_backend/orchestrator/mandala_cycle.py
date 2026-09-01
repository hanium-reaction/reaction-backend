"""만다라 축 → 다음 2주 계획 시드 (U14, ADR-0008 §3·§8 "G") — LLM 0콜, DB 무관 순수 함수.

만다라트를 그려 놓고 축을 승격까지 해도, 지금까지는 그 축으로 계획이 서지 않았다.
`POST /plans/generate` 의 시드는 **계획 인터뷰 outcome** 이고 그 안의 `is_heaviest` 는
인터뷰 당시 사용자가 고른 목표라, 만다라트를 다시 세워 축이 바뀌어도 계획은 여전히 옛
목표를 분해한다. 축 제목이 `goals.heaviest` **보기**로 들어가는 배선(ADR-0008 §8 "B")은
있지만 그건 *새 인터뷰를 다시 할 때* 얘기다.

이 모듈이 그 사이를 잇는다 — 이미 답해 둔 인터뷰 outcome 에서 **`core_goals` 만** 이 축으로
갈아끼우고, 그 축의 칸 8개를 마일스톤 뼈대로 넘긴다. 나머지(정체성·가용 시간·선호)는 사용자가
실제로 답한 값 그대로다. **지어내지 않는다** — 인터뷰가 없으면 호출자가 422 로 안내한다
(v2.01 의 교훈: 지어낸 답으로 슬롯을 닫느니 열어 두고 묻는 게 낫다).

지평 상한(2주)은 여기서 정하지 않는다. `core_goals[0].title` 이 승격된 목표 제목과 같으므로
기존 `routes/planning.py::_max_plan_weeks` 판정이 그대로 걸린다 — 새 규칙을 만들면 만다라
계획의 '2주'가 두 곳에서 따로 정의된다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from reaction_backend.db.models.behavioral_profile import BehavioralProfile
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.interaction_style import InteractionStyle
from reaction_backend.orchestrator import profile_memory
from reaction_backend.schemas.interview import GoalCandidate, InterviewOutcome
from reaction_backend.schemas.planning import MilestoneDraft

# 승격된 축은 이번 주기의 **유일한** 목표다 — 한 계획은 heaviest 하나만 분해·배치하므로
# (api-contract §8) 나머지를 실어 봐야 세션이 안 생기고 tier 게이트만 흔든다.
_AXIS_CATEGORY = "other"  # 만다라 축엔 category 개념이 없다(`promote_mandala_node` 와 동일)

type _Tier = Literal["focus", "maintain", "parked"]


def _tier(promoted: Goal) -> _Tier:
    """`goals.goal_tier` 는 DB 에서 str 로 온다 — 경계 스키마의 Literal 로 좁힌다."""
    return cast("_Tier", promoted.goal_tier)


def axis_goal_candidate(
    *, axis: GoalNode, promoted: Goal, template: GoalCandidate | None = None
) -> GoalCandidate:
    """축 + 승격된 Goal → 이번 주기의 heaviest 목표 후보.

    `template` 은 사용자가 인터뷰에서 **이 목표에 대해** 이미 답해 둔 값이 있으면 그것
    (제목이 같은 `core_goals` 항목) — 주당 시간·세션 길이·선호 시간대 같은 슬롯을 버리지
    않으려는 것이다. 없으면 축이 가진 것만으로 만든다(나머지는 None → 분해가 전역 기본을 쓴다).

    `deadline` 은 승격된 Goal 의 것을 그대로 쓴다 — 궁극목표 마감을 상속하지 않는다는 결정
    (api-contract §6 `POST /goals/ultimate`)을 여기서 뒤집지 않는다. 없으면 None 이고,
    그러면 지평이 상한(2주)으로 잡힌다.
    """
    base = template.model_copy(deep=True) if template is not None else None
    if base is None:
        return GoalCandidate(
            title=promoted.title,
            category=promoted.category or _AXIS_CATEGORY,
            is_heaviest=True,
            deadline=promoted.deadline.isoformat() if promoted.deadline is not None else None,
            why_now=axis.why_text,
            tentative_tier=_tier(promoted),
            confidence=1.0,  # 사용자가 직접 고른 축이다 — 추정이 아니다
        )
    return base.model_copy(
        update={
            "title": promoted.title,
            "is_heaviest": True,
            "deadline": (
                promoted.deadline.isoformat() if promoted.deadline is not None else base.deadline
            ),
            "why_now": base.why_now or axis.why_text,
            "tentative_tier": _tier(promoted),
        }
    )


def seed_outcome(*, base: InterviewOutcome, axis: GoalNode, promoted: Goal) -> InterviewOutcome:
    """인터뷰 outcome → 이 축 하나만 담은 시드. 정체성·가용 시간·선호는 원본 그대로.

    `core_goals` 를 통째로 갈아끼우는 이유: 한 계획은 heaviest 하나만 다루므로(api-contract
    §8) 다른 목표를 남겨 두면 세션도 안 생기면서 승인 시 `materialize_goals` 만 흔들고
    tier 게이트에 잡힐 수 있다. 다른 목표의 기존 Goal 행은 이미 영속돼 있어 사라지지 않는다.
    """
    template = next((g for g in base.core_goals if g.title.strip() == promoted.title.strip()), None)
    candidate = axis_goal_candidate(axis=axis, promoted=promoted, template=template)
    return base.model_copy(update={"core_goals": [candidate], "horizon": candidate.deadline})


def cells_as_milestones(cells: Sequence[GoalNode]) -> list[MilestoneDraft]:
    """축의 칸(depth=2) → 마일스톤 뼈대. 만다라트가 계획을 실제로 이끄는 지점이다.

    칸 제목이 곧 마일스톤 제목이다 — AI 가 다시 지어내게 두면 사용자가 만다라트에서 확정한
    분해를 계획이 무시하게 된다. 완료 표시된 칸은 뺀다(이미 끝낸 것을 다시 계획하지 않는다).
    `summary` 는 비운다 — 칸에는 요약에 해당하는 필드가 없고, 없는 문장을 지어내지 않는다.

    ADR-0007 §1 커서 모델이 이 목록 중 **앞쪽 일부만** 이번 2주에 담는다(나머지는 다음
    주기가 이어받는다) — 여기서 미리 자르지 않는다.
    """
    return [
        MilestoneDraft(title=c.title, summary="")
        for c in sorted(cells, key=lambda c: c.order_index)
        if c.completed_at is None
    ]


def slots_from_profile(
    *,
    behavioral: BehavioralProfile | None,
    interaction: InteractionStyle | None,
    focus_mode_prefs: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """온보딩 프로필 → `interview_adapter.build_outcome` 입력 슬롯.

    계획 인터뷰를 아직 안 한 사용자가 만다라 축으로 주기를 열 때의 **2순위 시드**다. 값을
    지어내는 게 아니라 사용자가 온보딩·설정에서 **직접 넣은 값**을 되돌린다 —
    `persist_profile_from_outcome` 이 인터뷰 답을 프로필에 저장하고 설정 화면
    (`routes/settings.py`)이 그걸 편집하므로, 프로필은 이 값들의 최신 진실이다.

    `profile_memory.seed_slots_from_profile`(재인터뷰 시드)를 그대로 쓰고 **활동창만 더한다**.
    그쪽이 활동창을 안 만드는 이유는 그 결과가 `interview_slot_answers` 에 UPSERT 되기 때문인데
    (옵션에 없는 표기가 '사용자의 답'으로 남는 사고, v2.01), 여기 결과는 이 계획 한 번을 위한
    임시 값이라 저장되지 않는다 — 그래서 안전하게 포함할 수 있다.

    채우지 못한 슬롯은 그냥 비운다. `build_outcome` 이 문서화된 기본값으로 채우고 그 키를
    `unresolved_slots` 에 남기므로, 무엇을 추정했는지가 응답에 그대로 드러난다.
    """
    slots = profile_memory.seed_slots_from_profile(
        behavioral=behavioral, interaction=interaction, focus_mode_prefs=focus_mode_prefs
    )
    if behavioral is not None:
        start, end = behavioral.preferred_start_time, behavioral.preferred_end_time
        if start is not None and end is not None:
            slots["time.activity_window"] = {
                "type": "range",
                "start": start.strftime("%H:%M"),
                "end": end.strftime("%H:%M"),
            }
    return slots


def has_usable_profile(behavioral: BehavioralProfile | None) -> bool:
    """이 프로필로 계획을 배치할 수 있는가 — **활동 시간대를 아는가**가 유일한 기준.

    피크 시간·집중 길이는 없으면 기본값으로 굴러가지만, 활동창은 "언제 배치해도 되는가" 라
    모르면 배치 자체가 추측이 된다. 그래서 여기가 비면 호출자가 422 로 인터뷰를 안내한다.
    """
    return (
        behavioral is not None
        and behavioral.preferred_start_time is not None
        and behavioral.preferred_end_time is not None
    )


__all__ = [
    "axis_goal_candidate",
    "cells_as_milestones",
    "has_usable_profile",
    "seed_outcome",
    "slots_from_profile",
]
