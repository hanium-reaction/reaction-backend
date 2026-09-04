"""영속된 Goal 하나 → 그 목표만 담은 First Plan 시드 — LLM 0콜, DB 무관 순수 함수.

`POST /plans/generate` 의 시드는 **계획 인터뷰 outcome** 이고, 그 안의 `is_heaviest` 는
인터뷰 당시 사용자가 고른 목표다. 그래서 목표를 여러 개 굴리는 사용자가 "목표 A 의 다음
주기" 를 열어도 **최근 인터뷰의 목표 B** 가 재투영됐다(#398). 이 모듈이 그 사이를 잇는다 —
이미 답해 둔 outcome 에서 **`core_goals` 만** 지정한 목표로 갈아끼우고, 나머지(정체성·가용
시간·선호)는 사용자가 실제로 답한 값 그대로 둔다. **지어내지 않는다.**

원래 `mandala_cycle` 안에 만다라 축 전용으로 있던 규칙이다. 축(`GoalNode`)은 #373 에서
`why_text` 읽기가 빠진 뒤로 **아무 값도 기여하지 않고** 승격된 `Goal` 만 쓰이고 있었다 —
즉 규칙은 처음부터 "축" 이 아니라 "목표" 단위였다. 그래서 여기로 옮겨 두 입구(만다라 축 ·
주간 리포트의 다음 주기 제안)가 **같은 한 벌**을 쓰게 한다.

지평 상한은 여기서 정하지 않는다. `core_goals[0].title` 이 대상 목표 제목과 같으므로 기존
`routes/planning.py::_max_plan_weeks` 판정(만다라 승격 목표 2주 / 그 외 4주, ADR-0008 §3)이
그대로 걸린다 — 새 규칙을 만들면 '2주' 가 두 곳에서 따로 정의된다.
"""

from __future__ import annotations

from typing import Literal, cast

from reaction_backend.db.models.goal import Goal
from reaction_backend.schemas.interview import GoalCandidate, InterviewOutcome

# 목표에 category 가 없을 때의 기본값 (`promote_mandala_node` 와 동일) — 만다라 축엔
# category 개념이 없어 승격된 목표가 이 값으로 남을 수 있다.
_DEFAULT_CATEGORY = "other"

type _Tier = Literal["focus", "maintain", "parked"]


def _tier(goal: Goal) -> _Tier:
    """`goals.goal_tier` 는 DB 에서 str 로 온다 — 경계 스키마의 Literal 로 좁힌다."""
    return cast("_Tier", goal.goal_tier)


def goal_candidate(*, goal: Goal, template: GoalCandidate | None = None) -> GoalCandidate:
    """영속된 Goal → 이번 주기의 heaviest 목표 후보.

    `template` 은 사용자가 인터뷰에서 **이 목표에 대해** 이미 답해 둔 값이 있으면 그것
    (제목이 같은 `core_goals` 항목) — 주당 시간·세션 길이·선호 시간대 같은 슬롯을 버리지
    않으려는 것이다. 없으면 Goal 이 가진 것만으로 만든다(나머지는 None → 분해가 전역 기본을
    쓴다).

    `deadline` 은 Goal 의 것을 그대로 쓴다 — 궁극목표 마감을 상속하지 않는다는 결정
    (api-contract §6 `POST /goals/ultimate`)을 여기서 뒤집지 않는다. 없으면 template 의 값,
    그것도 없으면 None 이고 그러면 지평이 상한으로 잡힌다.
    """
    base = template.model_copy(deep=True) if template is not None else None
    if base is None:
        return GoalCandidate(
            title=goal.title,
            category=goal.category or _DEFAULT_CATEGORY,
            is_heaviest=True,
            deadline=goal.deadline.isoformat() if goal.deadline is not None else None,
            tentative_tier=_tier(goal),
            confidence=1.0,  # 사용자가 직접 고른 목표다 — 추정이 아니다
        )
    return base.model_copy(
        update={
            "title": goal.title,
            "is_heaviest": True,
            "deadline": (goal.deadline.isoformat() if goal.deadline is not None else base.deadline),
            "tentative_tier": _tier(goal),
        }
    )


def seed_outcome(*, base: InterviewOutcome, goal: Goal) -> InterviewOutcome:
    """인터뷰 outcome → 이 목표 하나만 담은 시드. 정체성·가용 시간·선호는 원본 그대로.

    `core_goals` 를 통째로 갈아끼우는 이유: 한 계획은 heaviest 하나만 다루므로(api-contract
    §8) 다른 목표를 남겨 두면 세션도 안 생기면서 승인 시 `materialize_goals` 만 흔들고
    tier 게이트에 잡힐 수 있다. 다른 목표의 기존 Goal 행은 이미 영속돼 있어 사라지지 않는다.
    """
    template = next((g for g in base.core_goals if g.title.strip() == goal.title.strip()), None)
    candidate = goal_candidate(goal=goal, template=template)
    return base.model_copy(update={"core_goals": [candidate], "horizon": candidate.deadline})


__all__ = ["goal_candidate", "seed_outcome"]
