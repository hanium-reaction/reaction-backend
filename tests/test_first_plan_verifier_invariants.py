"""L1-7 선행 발견의 핀 테스트 — ④층 검토기가 검사해선 안 되는 것을 고정한다.

[`docs/experiments/rubric-first-plan-v1.md`](../docs/experiments/rubric-first-plan-v1.md) §1
이 "1,620건 전수 확인"을 근거로 삼는데, 그 근거가 스크립트 한 번 돌린 기록으로만 남으면
`session_min_for` 나 클램프를 누가 손대는 순간 **문서의 주장이 조용히 썩는다.** 여기서
불변식으로 고정한다.

고정하는 것 세 가지:

1. **③층 보정 후에는 `focus_capacity` 초과가 존재할 수 없다.** `normalize_action_minutes`
   의 상한과 `_review_variables` 가 검토기에 넘기는 `focus_capacity` 가 같은 함수
   (`session_min_for`)이기 때문이다. 따라서 `plan_quality` 의 "세션 길이 상한" 체크는
   **구조적으로 발화 불가**이고, 그 항목의 모든 반려는 정의상 오탐이다 — 실측 사고
   (120분 세션 3/3 반려 → 재분해가 60분으로 축소)의 뿌리.

2. **`focus_capacity` 는 `session_min_for` 그 자체다.** 둘이 갈리면 1번 불변식이 깨지므로
   프롬프트 변수와 클램프 상한이 같은 값임을 직접 못박는다.

3. **15분 미만은 버그가 아니라 의도다.** 주당 시간을 빈도로 나눈 평균이 15분 아래로
   내려가는 조합에서는 룰이 **일부러** 하한을 낮춘다(안 그러면 그 계획의 평균 길이를 어떤
   카드도 가질 수 없어 주당 예산이 카드마다 샌다). 누가 "9분 카드는 버그"라며 하한을
   15로 되돌리는 것을 막는다.

⚠️ **뮤테이션 가드가 같이 있다** (`test_grid_is_actually_adversarial`). 보정을 안 거친
원안이 실제로 상한을 넘는지 먼저 확인한다 — 이게 없으면 그리드가 작은 값만 만들어도
1번 테스트가 초록이라 `if False:` 로 바꾼 것과 구별되지 않는다.
"""

from __future__ import annotations

import itertools
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from reaction_backend.orchestrator import first_plan_adapter
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)
from reaction_backend.schemas.planning import ActionItemDraft, GoalDecomposition, GoalNodeDraft

KST = timezone(timedelta(hours=9))
TODAY = date(2026, 8, 31)  # 고정 — 현재시각을 쓰면 하루만 지나도 판정이 뒤집힌다

# 프로덕션이 실제로 쓰는 슬롯 값의 범위. `session_length_min` 은 인터뷰 칩 보기,
# `focus_duration_min` 은 전역 폴백, 주당시간·빈도는 사용자 자유 입력의 대표값이다.
_SESSION_LENS = [None, 15, 30, 50, 60, 90, 120, 180, 240]
_FOCUS_DURS = [15, 25, 50, 90, 120]
_WEEKLY_HOURS = [None, 1, 2, 6, 8, 20]
_FREQS = [None, 1, 2, 3, 5, 7]

# ActionItemDraft.estimated_minutes 의 스키마 상한이 240 이라 그 위는 애초에 못 들어온다.
_RAW_MINUTES = [3, 9, 15, 45, 60, 120, 200, 240]


def _outcome(
    *,
    session_length_min: int | None,
    focus_duration_min: int,
    weekly_hours: int | None,
    frequency_per_week: int | None,
    deadline_days: int = 120,
) -> InterviewOutcome:
    deadline = (TODAY + timedelta(days=deadline_days)).isoformat()
    return InterviewOutcome(
        session_id="t-verifier-invariants",
        generated_at=datetime.now(KST),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대3", season="방학"),
        core_goals=[
            GoalCandidate(
                title="테스트 목표",
                category="study",
                is_heaviest=True,
                tentative_tier="focus",
                confidence=0.9,
                deadline=deadline,
                session_length_min=session_length_min,
                weekly_hours=weekly_hours,
                frequency_per_week=frequency_per_week,
            )
        ],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:00"), peak_window=["저녁"]
        ),
        preferences=PreferenceProfile(
            recovery_tone="따뜻",
            rest_ok=True,
            downscope_unit_min=15,
            focus_duration_min=focus_duration_min,
        ),
        horizon=deadline,
    )


def _plan(minutes: list[int]) -> GoalDecomposition:
    nodes = [
        GoalNodeDraft(
            node_id="r",
            parent_id=None,
            title="root",
            node_type="root",
            order_index=0,
            is_leaf=False,
        )
    ]
    items = []
    for i, m in enumerate(minutes):
        nodes.append(
            GoalNodeDraft(
                node_id=f"l{i}",
                parent_id="r",
                title=f"세션{i}",
                node_type="leaf",
                order_index=i,
                is_leaf=True,
            )
        )
        items.append(
            ActionItemDraft(
                node_id=f"l{i}",
                title=f"세션{i}",
                estimated_minutes=m,
                category="study",
                first_step="시작하기",
            )
        )
    return GoalDecomposition(goal_nodes=nodes, action_items=items, policy_violations=[])


def _corrected(outcome: InterviewOutcome, plan: GoalDecomposition) -> GoalDecomposition:
    """`first_plan.decompose_goal` 과 **같은 순서**의 ③층 보정 체인.

    순서가 갈리면 이 테스트가 지키려는 게 프로덕션의 불변식이 아니게 된다 —
    `first_plan.py::decompose_goal` 을 고치면 여기도 같이 고쳐야 한다.

    ⚠️ 프로덕션 체인의 두 번째 단계 `drop_out_of_cycle_branches` 는 **여기 없다.** 이
    그리드는 마일스톤 없는 케이스만 만들고(`_outcome` 이 안 넘긴다), 그 함수는 마일스톤이
    없으면 아무것도 안 한다. 마일스톤을 쓰는 케이스를 이 파일에 추가한다면 그때 체인에도
    넣어야 한다 — 생성기 쪽(`build_golden_first_plan_cases._shaped`)은 2026-09-02 에
    `milestone_fixed` 블록이 마일스톤을 싣게 되면서 이미 넣었다.
    """
    plan, _ = first_plan_adapter.drop_waiting_steps(plan)
    plan = first_plan_adapter.shape_action_plan(outcome, "standard", plan, target_date=TODAY)
    return first_plan_adapter.extend_action_plan_to_horizon(
        outcome, "standard", plan, target_date=TODAY
    )


def _grid() -> list[tuple[int | None, int, int | None, int | None]]:
    return list(itertools.product(_SESSION_LENS, _FOCUS_DURS, _WEEKLY_HOURS, _FREQS))


def _raw_minutes_for(seed: int) -> list[int]:
    rng = random.Random(seed)
    return [rng.choice(_RAW_MINUTES) for _ in range(rng.randint(3, 12))]


# ── 1. 상한 초과는 검토기에 도달할 수 없다 ────────────────────────────────


def test_no_action_item_exceeds_focus_capacity_after_correction() -> None:
    """③층 보정 후 `focus_capacity` 초과 항목은 **한 건도** 남지 않는다.

    이게 참인 한 `plan_quality` 의 '세션 길이 상한' 체크는 발화할 수 없고, 그 항목의
    반려는 전부 오탐이다 (루브릭 §1.1).
    """
    offenders: list[tuple[object, ...]] = []
    for seed, (sl, fd, wh, fq) in enumerate(_grid()):
        outcome = _outcome(
            session_length_min=sl,
            focus_duration_min=fd,
            weekly_hours=wh,
            frequency_per_week=fq,
        )
        capacity = first_plan_adapter.session_min_for(outcome)
        plan = _corrected(outcome, _plan(_raw_minutes_for(seed)))
        over = sorted(
            {a.estimated_minutes for a in plan.action_items if a.estimated_minutes > capacity}
        )
        if over:
            offenders.append((sl, fd, wh, fq, capacity, over))

    assert offenders == [], (
        f"③층 보정 후에도 상한을 넘는 조합이 {len(offenders)}건 있다 — 루브릭 §1.1 의 "
        f"'구조적으로 발화 불가' 주장이 더 이상 참이 아니다. 예: {offenders[:3]}"
    )


def test_grid_is_actually_adversarial() -> None:
    """뮤테이션 가드 — 보정을 **안 거치면** 그리드가 실제로 상한을 넘는다.

    이 테스트가 없으면 위 테스트는 그리드가 작은 값만 만들어도 초록이라, 보정이 진짜로
    일하고 있는지 구별하지 못한다(`eval/README.md` 가 회복 골든셋에서 요구한 것과 같은 가드).
    """
    violating = 0
    for seed, (sl, fd, wh, fq) in enumerate(_grid()):
        outcome = _outcome(
            session_length_min=sl,
            focus_duration_min=fd,
            weekly_hours=wh,
            frequency_per_week=fq,
        )
        capacity = first_plan_adapter.session_min_for(outcome)
        raw = _plan(_raw_minutes_for(seed))  # 보정 없음
        if any(a.estimated_minutes > capacity for a in raw.action_items):
            violating += 1

    assert violating > len(_grid()) // 2, (
        f"원안이 상한을 넘는 조합이 {violating}건뿐이다 — 그리드가 충분히 적대적이지 않아 "
        "위 불변식 테스트가 무의미해진다."
    )


# ── 2. 프롬프트 변수와 클램프 상한이 같은 값이다 ──────────────────────────


@pytest.mark.parametrize(
    ("session_length_min", "focus_duration_min"),
    [(None, 50), (15, 90), (120, 25), (240, 120)],
)
def test_focus_capacity_variable_is_the_clamp_ceiling(
    session_length_min: int | None, focus_duration_min: int
) -> None:
    """검토기에 넘기는 `focus_capacity` 는 `session_min_for` 그 자체다.

    둘이 갈리는 순간 위 불변식이 깨진다. `_review_variables` 는 `planning_context` 의
    `prompt_vars` 를 그대로 읽으므로 여기서 그 값을 확인하면 된다.
    """
    outcome = _outcome(
        session_length_min=session_length_min,
        focus_duration_min=focus_duration_min,
        weekly_hours=6,
        frequency_per_week=3,
    )
    ctx = first_plan_adapter.context_from_outcome(outcome, density="standard", target_date=TODAY)
    capacity = first_plan_adapter.session_min_for(outcome)

    assert ctx["prompt_vars"]["focus_capacity"] == f"{capacity}분"


# ── 3. 15분 미만은 버그가 아니라 의도다 ───────────────────────────────────


def test_sub_15_minute_sessions_are_intentional_not_a_defect() -> None:
    """주당 시간을 빈도로 나눈 평균이 15분 아래인 조합에서는 하한이 **의도적으로** 낮아진다.

    하한을 15로 되돌리면 그 계획의 평균 길이(`planned_session_min_for`)를 어떤 카드도
    가질 수 없어 주당 예산이 카드마다 1.5배씩 샌다 — `normalize_action_minutes` 주석의 사유.
    검토기가 이걸 반려하면 오탐이다 (루브릭 §2 D-없음 / §3 무결함 대조군).
    """
    # 주당 1시간 + 주 7회 → 평균 60/7 ≈ 9분 → 하한 10분(_MIN_PLANNED_SESSION_MIN)
    outcome = _outcome(
        session_length_min=None,
        focus_duration_min=50,
        weekly_hours=1,
        frequency_per_week=7,
    )
    planned = first_plan_adapter.planned_session_min_for(outcome)
    assert planned < 15, "이 조합의 평균 세션 길이가 15분 이상이면 픽스처가 낡았다"

    # 하한은 값을 **끌어올릴** 때만 작동한다 — 15분보다 짧은 원안을 줘야 드러난다.
    plan = first_plan_adapter.shape_action_plan(
        outcome, "standard", _plan([3, 9, 12]), target_date=TODAY
    )
    minutes = {a.estimated_minutes for a in plan.action_items}

    assert min(minutes) < 15, (
        f"15분 미만 카드가 사라졌다({sorted(minutes)}) — 하한이 15로 되돌아갔다면 "
        "이 계획의 평균 길이를 어떤 카드도 못 갖는다. 루브릭 §1.1 표의 90건 계열을 확인할 것."
    )
    # 하한 = min(15, 평균, 상한). 이 조합에서는 평균(10)이 이겨 9분 garbage 가 10분으로 올라간다.
    assert min(minutes) == min(15, planned, first_plan_adapter.session_min_for(outcome))


# 루브릭 §1.1 표 2번 행. `planned_session_min_for < 15` 는 슬롯만으로 결정되는 값이라
# 그리드 전수를 셀 수 있다 — 위 테스트가 보는 조합 **1개**와 달리 표의 숫자 자체를 고정한다.
_SUB15_COMBOS = 90


def test_sub_15_floor_count_over_the_whole_grid() -> None:
    """루브릭 §1.1 표 2번 행의 **숫자**를 그리드 전수로 고정한다.

    ⚠️ 이 핀이 없어서 표에 **89** 라는 틀린 값이 들어갔고, 거기서 실험계획서 §2 ·
    바로 위 테스트의 docstring · **프로덕션 소스 주석**(`orchestrator/first_plan.py`
    `_review_variables`)까지 네 곳으로 퍼졌다(2026-09-02 감사). 산문으로만 있는 수치는
    조용히 썩는다 — 루브릭이 "이 표는 핀 테스트로 고정돼 있다"고 주장하는 동안 실제로는
    1번 행만 고정돼 있었다.

    2번 행이 1번 행과 다른 점: 1번("상한 초과 0건")은 클램프 상한과 `focus_capacity` 가
    **같은 함수**라서 나오는 구조적 0 이고, 2번은 그냥 세어야 아는 값이다. 그래서 값이
    바뀌는 것 자체는 결함이 아니다 — `_MIN_PLANNED_SESSION_MIN` 이나 `planned_session_min_for`
    를 의도적으로 바꿨다면 이 상수와 루브릭 표를 **함께** 고치면 된다.
    """
    sub15 = [
        (sl, fd, wh, fq)
        for sl, fd, wh, fq in _grid()
        if first_plan_adapter.planned_session_min_for(
            _outcome(
                session_length_min=sl,
                focus_duration_min=fd,
                weekly_hours=wh,
                frequency_per_week=fq,
            )
        )
        < 15
    ]
    assert len(sub15) == _SUB15_COMBOS, (
        f"15분 미만 조합이 {len(sub15)}건이다 (고정값 {_SUB15_COMBOS}). 하한 규칙을 "
        "의도적으로 바꿨다면 이 상수와 rubric-first-plan-v1.md §1.1 표 2번 행을 함께 고칠 것."
    )
