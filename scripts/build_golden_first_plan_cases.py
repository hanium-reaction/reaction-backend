"""첫 계획 골든셋 84건 생성기 (L1-7).

`docs/experiments/experiment-plan-v1.md` §2 L1-7 과
[`rubric-first-plan-v1.md`](../docs/experiments/rubric-first-plan-v1.md) §3 의 사양을 구현한다.

| 블록 | kind | 건수 | 무엇을 보는가 |
|---|---|---|---|
| `normal`               | decompose | 12 | 6목표 × 마감 2종 — L1-7A 기준선(M17~M26) |
| `constraint_edge`      | decompose | 12 | 집중 용량 ±5분 격자 (10/15/20 · 45/50/55 · 85/90/95 · 115/120/125) |
| `milestone_fixed`      | decompose |  6 | 외부 고정 날짜 + **확정 마일스톤 3~4개** — 커버리지(M22)·배치(M21) + **M23·M24 의 유일한 입력** |
| `busy_saturated`       | decompose |  4 | 요구량이 가용량에 붙거나 넘는 포화 — 분량 절단(M19) |
| `defect_free_control`  | verify    | 30 | **M29 `false_reject_rate` 의 분모.** 반려하면 전부 오탐. 12→30 으로 늘린 이유는 rule of three — 12건에서는 0건 반려여도 95% 상한이 25% 라 사전등록 임계값 ≤0.10 을 확인할 수 없다 |
| `seeded_defect`        | verify    | 20 | 2기준계획 × D1~D5 × easy/boundary — M27·M28 |
| **합계**               |           | **84** | |

## 두 종류의 케이스가 한 파일에 있다 (`kind`)

- **`decompose`** — 인터뷰 슬롯만 담는다. 하네스가 ②③층을 실제로 태워 계획을 만든다(L1-7A).
- **`verify`** — **완성된 계획을 담는다.** 하네스는 그걸 ④층 검토기에만 먹인다(L1-7B).
  검토기를 재려면 입력 계획이 고정돼야 한다 — 매번 새로 분해하면 검토기 성능과 분해기
  변동이 한 수치에 섞인다.

## 결함은 이 파일이 만들지 않는다 — `eval/first_plan_seeded_defects.json` 을 읽는다

계획서 L1-7B 의 **held-out fault design** 요구(체크리스트를 쓴 사람이 심을 결함까지
고르면 "자기가 만든 버그만 잡는 린터"가 된다)를 구조로 지킨다:

- 루브릭(`rubric-first-plan-v1.md`)을 쓴 주체가 **결함 인스턴스를 쓰지 않는다.**
- 결함 내용은 **다른 모델**이 D1~D5 의 한 줄 정의와 기준 계획 JSON 만 보고 작성해
  `eval/first_plan_seeded_defects.json` 에 커밋된다(작성 조건은 그 파일의 `provenance`).
- 이 생성기는 그 파일의 **연산(op)을 결정적으로 적용**하기만 한다.

⚠️ **완화이지 해소가 아니다.** 결함 **분류 체계(D1~D5)** 자체는 여전히 루브릭 작성자의
것이다. 분류 밖의 실패 유형은 이 골든셋으로 영원히 안 잡힌다(루브릭 §6).

## 왜 기준 계획을 손으로 안 쓰고 ③층에 태우는가

`verify` 블록의 계획은 손으로 쓴 원안을 **프로덕션과 같은 ③층 보정 체인**에 통과시킨
결과다. 검토기가 운영에서 실제로 보는 것이 ③층 **출력**이기 때문이다. 손으로 쓴 계획을
먹이면 `N회차` 채움 세션이나 클램프된 길이 같은, **오탐이 가장 잘 나는 산물**이 골든셋에
아예 없게 된다 — 그게 바로 120분 사고의 형태였다.

**난수도 현재시각도 쓰지 않는다.** 같은 커밋은 항상 같은 파일을 만들고,
`tests/test_golden_first_plan_cases.py::test_file_on_disk_matches_the_generator` 가
**커밋된 파일 == 생성기 출력**을 고정한다.

**정직성**: 전 케이스 `synthetic: true`. 실사용 인터뷰 분포를 대표하지 않는다.

실행:
  uv run python -m scripts.build_golden_first_plan_cases
  uv run python -m scripts.build_golden_first_plan_cases --stdout
  uv run python -m scripts.build_golden_first_plan_cases --dump-base-plans  # held-out 의뢰용
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

from reaction_backend.orchestrator import first_plan_adapter
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)
from reaction_backend.schemas.planning import (
    ActionItemDraft,
    GoalDecomposition,
    GoalNodeDraft,
    MilestoneDraft,
)

_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = _ROOT / "eval" / "golden_first_plan_cases.jsonl"
DEFECTS_PATH = _ROOT / "eval" / "first_plan_seeded_defects.json"

EXPECTED_TOTAL = 84

EXPECTED_COUNTS = {
    "normal": 12,
    "constraint_edge": 12,
    "milestone_fixed": 6,
    "busy_saturated": 4,
    "defect_free_control": 30,
    "seeded_defect": 20,
}

# 생성 시점 고정 날짜. 케이스에는 **상대 오프셋만** 저장하고(`deadline_offset_days`),
# 이 날짜는 ③층을 돌려 `verify` 계획을 굽는 데만 쓴다. 하네스가 실행일 + 같은 오프셋으로
# outcome 을 만들면 마감까지 남은 일수가 같아 계획도 같게 재현된다.
# (절대 날짜를 케이스에 넣으면 하루만 지나도 '마감 임박'이 '마감 지남'이 된다 — `s10_corners.py` 전례.)
BASE_DATE = date(2026, 9, 1)
KST = timezone(timedelta(hours=9))

# `defect_free_control` 이 반드시 덮어야 하는 회귀 속성 (루브릭 §3).
# 테스트가 이 4개가 블록 안에 **전부** 존재하는지 확인한다.
REQUIRED_CONTROL_PROPERTIES = (
    "session_equals_capacity",  # 세션 길이 == focus_capacity — 120분 사고 회귀
    "sub_15_is_normal",  # planned_session_min_for < 15 라 10~12분 카드가 정상
    "has_repeat_sessions",  # 룰이 붙인 `N회차` 연속 세션 — D1 오탐 회귀
    "mixed_lengths",  # 15분 확인 작업 + 긴 초안 혼재 — v3 가 반려하던 정상 편차
)

DEFECT_CODES = ("D1", "D2", "D3", "D4", "D5")
DEFECT_LEVELS = ("easy", "boundary")


# ── 인터뷰 슬롯 ───────────────────────────────────────────────────────────


class Slots(NamedTuple):
    """한 케이스의 인터뷰 응답. 이 값들이 L1-7A 의 **정답**이다 — 설계자가 아니라 사용자가 말한 값."""

    key: str
    title: str
    category: str
    success_image: str
    current_level: str
    deadline_offset_days: int
    session_length_min: int | None
    weekly_hours: int | None
    frequency_per_week: int | None
    focus_duration_min: int = 50
    role: str = "대학생"
    season: str = "학기 중"
    preferred_time: str = "저녁"
    approach_note: str | None = None
    # 사용자가 Stage B 에서 확정한 중간 목표 전체 목록(`FirstPlanState["milestones"]`).
    # 비어 있으면 마일스톤 없이 세우는 계획이고, `drop_out_of_cycle_branches` 도 안 돈다.
    milestones: tuple[tuple[str, str], ...] = ()  # (title, summary)
    milestone_cursor: int = 0  # 이미 끝낸 개수 — 이번 주기는 여기서부터 본다


def _outcome(slots: Slots, *, base_date: date = BASE_DATE) -> InterviewOutcome:
    deadline = (base_date + timedelta(days=slots.deadline_offset_days)).isoformat()
    return InterviewOutcome(
        session_id=f"golden-{slots.key}",
        generated_at=datetime(base_date.year, base_date.month, base_date.day, 9, 0, tzinfo=KST),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role=slots.role, season=slots.season),
        core_goals=[
            GoalCandidate(
                title=slots.title,
                category=slots.category,
                is_heaviest=True,
                tentative_tier="focus",
                confidence=0.9,
                deadline=deadline,
                success_image=slots.success_image,
                current_level=slots.current_level,
                session_length_min=slots.session_length_min,
                weekly_hours=slots.weekly_hours,
                frequency_per_week=slots.frequency_per_week,
                preferred_time=slots.preferred_time,
                approach_note=slots.approach_note,
            )
        ],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:00"),
            peak_window=[slots.preferred_time],
        ),
        preferences=PreferenceProfile(
            recovery_tone="담백",
            rest_ok=True,
            downscope_unit_min=15,
            focus_duration_min=slots.focus_duration_min,
        ),
        horizon=deadline,
    )


def _interview_payload(slots: Slots) -> dict[str, Any]:
    """케이스에 저장하는 인터뷰 슬롯 — 하네스가 이걸로 `InterviewOutcome` 을 재조립한다."""
    return {
        "role": slots.role,
        "season": slots.season,
        "focus_duration_min": slots.focus_duration_min,
        "preferred_time": slots.preferred_time,
        "goal": {
            "title": slots.title,
            "category": slots.category,
            "success_image": slots.success_image,
            "current_level": slots.current_level,
            "deadline_offset_days": slots.deadline_offset_days,
            "session_length_min": slots.session_length_min,
            "weekly_hours": slots.weekly_hours,
            "frequency_per_week": slots.frequency_per_week,
            "approach_note": slots.approach_note,
        },
        # 사용자가 Stage B 에서 확정한 중간 목표. 하네스는 이걸 `FirstPlanState["milestones"]`
        # 와 `["milestone_cursor"]` 로 넣는다 — `_cycle_milestones` 가 거기서 이번 주기 구간을
        # 잘라 분해 프롬프트와 `drop_out_of_cycle_branches` 에 **같은 목록**을 넘긴다.
        # ⚠️ 커서 앞쪽은 "이미 끝낸 단계" 라 다시 시키면 안 되고, 구간 뒤쪽은 "다음 주기가
        # 받을 단계" 라 여기서 시작하면 안 된다 — M24 가 그걸 잰다.
        "milestones": [{"title": t, "summary": summ} for t, summ in slots.milestones],
        "milestone_cursor": slots.milestone_cursor,
    }


# ── 손으로 쓴 원안 계획 (③층에 태우기 전) ────────────────────────────────


class Step(NamedTuple):
    """원안의 leaf 한 개."""

    branch: str
    title: str
    minutes: int
    first_step: str


def _raw_plan(steps: list[Step], *, goal_title: str, category: str) -> GoalDecomposition:
    """branch 로 묶인 root → branch → leaf 트리를 만든다."""
    nodes = [
        GoalNodeDraft(
            node_id="root",
            parent_id=None,
            title=goal_title,
            node_type="root",
            order_index=0,
            is_leaf=False,
        )
    ]
    branches: dict[str, str] = {}
    for step in steps:
        if step.branch not in branches:
            bid = f"b{len(branches) + 1}"
            branches[step.branch] = bid
            nodes.append(
                GoalNodeDraft(
                    node_id=bid,
                    parent_id="root",
                    title=step.branch,
                    node_type="branch",
                    order_index=len(branches) - 1,
                    is_leaf=False,
                )
            )

    items = []
    for i, step in enumerate(steps):
        leaf_id = f"l{i + 1}"
        nodes.append(
            GoalNodeDraft(
                node_id=leaf_id,
                parent_id=branches[step.branch],
                title=step.title,
                node_type="leaf",
                order_index=i,
                is_leaf=True,
            )
        )
        items.append(
            ActionItemDraft(
                node_id=leaf_id,
                title=step.title,
                estimated_minutes=step.minutes,
                category=category,
                first_step=step.first_step,
            )
        )
    return GoalDecomposition(goal_nodes=nodes, action_items=items, policy_violations=[])


def _shaped(
    outcome: InterviewOutcome,
    raw: GoalDecomposition,
    *,
    cycle_milestones: Sequence[MilestoneDraft] = (),
) -> GoalDecomposition:
    """`first_plan.decompose_goal` 과 **같은 순서**의 ③층 보정 체인.

    순서가 갈리면 골든셋의 계획이 검토기가 운영에서 보는 것과 달라진다.
    `tests/test_first_plan_verifier_invariants.py::_corrected` 와 같은 체인을 쓴다 —
    `first_plan.py::decompose_goal` 을 고치면 두 곳 다 고쳐야 한다.

    ⚠️ **`drop_out_of_cycle_branches` 는 여기 있지만 지금은 한 번도 안 돈다.**
    호출자가 둘뿐인데(`base_plans()` 와 결함 주입) 둘 다 `verify` 블록용이고, `verify`
    케이스에는 마일스톤이 없어서 `cycle_milestones` 가 항상 비어 있다. 실측: 전체 생성
    66건에서 이 함수 호출 **0회**, 이 단계를 지워도 산출물이 바이트 단위로 같다.

    ⚠️ **그래도 남겨 둔다** — 다만 "고쳤다" 고 말하지 않는다. 마일스톤은
    `milestone_fixed`(=`decompose` 블록)에 들어갔고 그 블록은 `_shaped` 를 아예 안 탄다.
    즉 2026-09-02 커밋이 "체인을 고쳤다" 고 쓴 것은 **틀렸다**(감사 5차). 이 단계가 실제로
    필요해지는 것은 **마일스톤을 가진 `verify` 케이스를 만드는 날**이고, 그때 이 자리가
    비어 있으면 감사 3차가 예고한 그대로 조용히 틀려진다. 미리 채워 둔 것이지 지금 무언가를
    고치고 있는 게 아니다.

    ⚠️ 그리고 **창(window)을 만드는 함수가 여기 없다.** 프로덕션은
    `_cycle_milestones(state)` 로 자른 목록을 넘기는데, 이 생성기에는 그 계산이 없어
    호출자가 직접 잘라 넘겨야 한다. 지금은 호출자가 없어 문제가 안 되지만, 마일스톤을 가진
    `verify` 케이스를 만들 때 **여기서 자르지 않으면 프로덕션과 다른 것을 넘기게 된다.**

    프로덕션 순서(`decompose_goal`):
        drop_waiting_steps → drop_out_of_cycle_branches → shape_action_plan
        → extend_action_plan_to_horizon

    ⚠️ 두 번째 단계는 **되채울 수 있을 때만** 돈다 — `extend_action_plan_to_horizon` 이
    케이던스를 명시한 목표(`frequency_per_week`)에만 회차를 붙이기 때문이다. 그 조건을
    빼먹고 걷어내면 4주 계획이 이틀치로 무너진다(프로덕션 주석의 실측: 12세션 → 3세션).
    """
    plan, _ = first_plan_adapter.drop_waiting_steps(raw)
    heaviest = next((g for g in outcome.core_goals if g.is_heaviest), outcome.core_goals[0])
    can_refill = bool((heaviest.frequency_per_week or 0) > 0)
    if cycle_milestones and can_refill:
        plan, _dropped = first_plan_adapter.drop_out_of_cycle_branches(plan, cycle_milestones)
    plan = first_plan_adapter.shape_action_plan(outcome, "standard", plan, target_date=BASE_DATE)
    return first_plan_adapter.extend_action_plan_to_horizon(
        outcome, "standard", plan, target_date=BASE_DATE
    )


def _renumber_leaves(plan: GoalDecomposition) -> tuple[GoalDecomposition, dict[str, str]]:
    """LLM 쪽 leaf 의 `node_id` 를 트리 순서대로 `l1..lN` 으로 다시 매긴다.

    ⚠️ **주입 지점을 지우기 위한 것이다.** 결함 파일은 새 노드에 `x5`·`x6` 같은 id 를
    주는데, 기준 계획의 leaf 는 전부 `l1..l4` 이고 룰 채움은 `tmp-continue-N` 이다.
    그대로 두면 계획 안에서 **`x` 로 시작하는 노드가 정확히 주입된 카드 하나**라,
    검토기가 내용을 전혀 안 읽고 `node_id` 모양만 보고 M28(주입 지점 지목)을 맞힌다 —
    8개 `insert_item` 케이스 전부에서. 2026-09-02 감사에서 나온 최대 누출이다.

    `root`·`b*`·`tmp-continue-*` 는 그대로 둔다. 앞의 둘은 leaf 가 아니고,
    `tmp-continue-*` 는 **프로덕션이 실제로 그 id 로 만든다**
    (`first_plan_adapter.extend_action_plan_to_horizon`) — 바꾸면 오히려 어긋난다.

    반환하는 매핑으로 `target_node_ids`(정답 키)도 함께 옮겨야 한다.
    """
    mapping: dict[str, str] = {}
    counter = 0
    for node in plan.goal_nodes:
        if not node.is_leaf or node.node_id.startswith("tmp-continue"):
            continue
        counter += 1
        mapping[node.node_id] = f"l{counter}"

    nodes = [n.model_copy(deep=True) for n in plan.goal_nodes]
    items = [a.model_copy(deep=True) for a in plan.action_items]
    for node in nodes:
        node.node_id = mapping.get(node.node_id, node.node_id)
        if node.parent_id is not None:
            node.parent_id = mapping.get(node.parent_id, node.parent_id)
    for item in items:
        item.node_id = mapping.get(item.node_id, item.node_id)
    return GoalDecomposition(goal_nodes=nodes, action_items=items, policy_violations=[]), mapping


def _plan_payload(plan: GoalDecomposition) -> dict[str, Any]:
    return {
        "goal_nodes": [n.model_dump(mode="json", by_alias=False) for n in plan.goal_nodes],
        "action_items": [a.model_dump(mode="json", by_alias=False) for a in plan.action_items],
    }


# ── decompose 블록의 슬롯 표 (손으로 작성) ───────────────────────────────

# ⚠️ **6목표 중 3개는 일부러 "주당 시간 ÷ 빈도 < 세션 길이"** 로 잡았다
# (jeongcheogi 2h/3회=40분 < 50 · toeic 2h/5회=24분 < 30 · portfolio 2h/2회=60분 < 90).
#
# 왜: `planned_session_min_for` 가 `min(주당분/빈도, 집중용량)` 이라, 주당 시간이 넉넉하면
# **평균과 상한이 같은 값으로 프롬프트에 인쇄된다.** 그러면 LLM 이 개수를 지키고 상한만
# 안 넘겨도 M18(분량 비율)이 **산술적으로 1.0 을 넘을 수 없다** — 2026-09-02 1차 실행에서
# 34케이스 중 32개가 그 상태였고, 102행 중 88행이 1.0 초과 불가였다. 그 상태로 낸
# "과소 생성 85/102" 는 모델의 행동이 아니라 **슬롯 구성의 성질**이었다
# (`docs/experiments/l1-7-results.md` §6.1).
#
# 상한이 안 걸리는 조합을 섞어야 M18 이 **양방향 지표**가 된다. `test_m18_is_two_sided...`
# 가 이 성질을 고정한다.
_NORMAL_GOALS: tuple[Slots, ...] = (
    Slots(
        key="normal-jeongcheogi",
        title="정보처리기사 실기 합격",
        category="career",
        success_image="실기 시험에서 60점을 넘겨 합격증을 받는 것",
        current_level="필기는 붙었고 실기는 아직 손도 못 댔어요.",
        deadline_offset_days=28,
        session_length_min=50,
        weekly_hours=2,
        frequency_per_week=3,
    ),
    Slots(
        key="normal-toeic",
        title="토익 800점 넘기기",
        category="study",
        success_image="다음 정기시험에서 800점 이상 성적표를 받는 것",
        current_level="작년에 690점 받고 손 놨어요.",
        deadline_offset_days=28,
        session_length_min=30,
        weekly_hours=2,
        frequency_per_week=5,
    ),
    Slots(
        key="normal-portfolio",
        title="개발 포트폴리오 사이트 완성",
        category="project",
        success_image="프로젝트 3개가 정리된 사이트를 배포하고 링크를 이력서에 넣는 것",
        current_level="리액트로 화면 몇 개 만들어봤어요.",
        deadline_offset_days=28,
        session_length_min=90,
        weekly_hours=2,
        frequency_per_week=2,
        approach_note="완성도보다 개수를 우선하고 싶어요.",
    ),
    Slots(
        key="normal-running",
        title="10km 논스톱으로 달리기",
        category="health",
        success_image="쉬지 않고 10km를 완주하는 것",
        current_level="3km부터 숨이 찹니다.",
        deadline_offset_days=28,
        session_length_min=None,
        weekly_hours=3,
        frequency_per_week=3,
        focus_duration_min=50,
    ),
    Slots(
        key="normal-thesis",
        title="졸업논문 1차 초안 제출",
        category="study",
        success_image="지도교수님께 서론부터 방법까지 초안을 보내는 것",
        current_level="주제만 정했고 참고문헌은 절반쯤 모았어요.",
        deadline_offset_days=28,
        session_length_min=120,
        weekly_hours=8,
        frequency_per_week=4,
        season="방학",
    ),
    Slots(
        key="normal-guitar",
        title="기타로 좋아하는 곡 한 곡 완주",
        category="self_dev",
        success_image="코드 안 보고 한 곡을 처음부터 끝까지 치는 것",
        current_level="코드 4개 정도 잡을 줄 압니다.",
        deadline_offset_days=28,
        session_length_min=25,
        weekly_hours=None,
        frequency_per_week=5,
    ),
)

# 마감 2종 — 4주(근접) / 10주(원거리). 지평 길이가 `extend_action_plan_to_horizon` 의
# 채움 분량과 `_MAX_PLAN_WEEKS` 절단을 모두 건드리므로 두 벌 다 필요하다.
_NORMAL_HORIZONS = ((28, "near"), (70, "far"))

# 집중 용량 ±5분 격자 (계획서 L1-7B "경계값 격자 필수").
# 15분은 `session_min_for` 의 하한이라 10 은 15로 끌어올려진다 — 그 경로도 격자에 넣는다.
_EDGE_ANCHORS = (15, 50, 90, 120)
_EDGE_OFFSETS = (-5, 0, 5)

_EDGE_BASE = Slots(
    key="edge",
    title="빅데이터분석기사 필기 합격",
    category="career",
    success_image="필기 과목별 40점 이상 + 평균 60점을 넘기는 것",
    current_level="통계는 수업에서 들었고 나머지는 처음이에요.",
    deadline_offset_days=42,
    session_length_min=50,
    weekly_hours=6,
    frequency_per_week=3,
)

_MILESTONE_GOALS: tuple[Slots, ...] = (
    Slots(
        key="milestone-exam",
        title="한국사능력검정 1급 취득",
        category="career",
        success_image="정해진 시험일에 응시해 1급을 받는 것",
        current_level="교재 1권을 절반 봤어요.",
        deadline_offset_days=35,
        session_length_min=60,
        weekly_hours=6,
        frequency_per_week=3,
        milestones=(
            ("전근대사 통사 1회독", "선사~조선 후기를 교재 1권으로 한 번 훑는다"),
            ("근현대사 통사 1회독", "개항기~현대를 교재 2권으로 한 번 훑는다"),
            ("기출 5회분 풀고 오답 정리", "최근 5회 기출을 풀고 틀린 문항을 유형별로 묶는다"),
        ),
    ),
    Slots(
        key="milestone-contest",
        title="교내 창업경진대회 본선 진출",
        category="project",
        success_image="마감일까지 사업계획서와 발표자료를 제출하는 것",
        current_level="아이디어만 있고 문서는 없습니다.",
        deadline_offset_days=21,
        session_length_min=90,
        weekly_hours=9,
        frequency_per_week=3,
        milestones=(
            (
                "문제 정의와 시장 조사 정리",
                "누구의 어떤 문제인지와 경쟁 서비스를 한 장으로 정리한다",
            ),
            ("사업계획서 초안 작성", "제출 양식에 맞춰 전 항목을 채운 초안을 만든다"),
            ("발표자료 제작과 리허설", "10분 발표용 슬라이드를 만들고 시간을 재며 연습한다"),
        ),
    ),
    Slots(
        key="milestone-conference",
        title="학부생 학술대회 포스터 발표",
        category="study",
        success_image="포스터를 인쇄해 발표장에 서는 것",
        current_level="실험 데이터는 다 모았고 정리가 안 됐어요.",
        deadline_offset_days=49,
        session_length_min=120,
        weekly_hours=8,
        frequency_per_week=2,
        season="방학",
        milestones=(
            ("실험 데이터 정리와 통계 처리", "수집한 데이터를 분석 가능한 형태로 정리한다"),
            ("그림과 표 초안 완성", "포스터에 들어갈 figure 를 초안 수준으로 만든다"),
            ("포스터 레이아웃 배치", "제목·초록·그림·결론을 한 판에 배치한다"),
            ("인쇄본 교정과 출력", "오탈자를 잡고 인쇄소에 넘긴다"),
        ),
        milestone_cursor=1,
    ),
    Slots(
        key="milestone-visa",
        title="교환학생 서류 제출 완료",
        category="career",
        success_image="마감 전에 지원 포털에 모든 서류를 올리는 것",
        current_level="어학성적만 있고 나머지는 아직입니다.",
        deadline_offset_days=30,
        session_length_min=45,
        weekly_hours=3,
        frequency_per_week=2,
        milestones=(
            ("학업계획서 작성", "지원 동기와 수학 계획을 요구 분량에 맞춰 쓴다"),
            ("추천서 요청과 회수", "지도교수께 요청하고 마감 전에 받는다"),
            ("증빙서류 발급과 업로드", "성적증명·재학증명을 떼어 포털에 올린다"),
        ),
    ),
    Slots(
        key="milestone-recital",
        title="동아리 정기공연 무대 서기",
        category="self_dev",
        success_image="공연 당일 두 곡을 실수 없이 연주하는 것",
        current_level="한 곡은 되고 한 곡은 절반입니다.",
        deadline_offset_days=56,
        session_length_min=60,
        weekly_hours=4,
        frequency_per_week=2,
        milestones=(
            ("1번 곡 암보 완성", "악보 없이 처음부터 끝까지 친다"),
            ("2번 곡 후반부 익히기", "절반만 되는 곡의 나머지를 손에 붙인다"),
            ("두 곡 이어서 합주 연습", "무대 순서대로 끊지 않고 연주한다"),
        ),
    ),
    Slots(
        key="milestone-defense",
        title="캡스톤 중간발표 통과",
        category="project",
        success_image="중간발표에서 데모를 돌리고 질의응답을 마치는 것",
        current_level="백엔드만 되고 화면이 없어요.",
        deadline_offset_days=24,
        session_length_min=120,
        weekly_hours=12,
        frequency_per_week=4,
        milestones=(
            ("구현 진척 정리", "지금까지 만든 것을 데모 가능한 상태로 묶는다"),
            ("중간발표 자료 작성", "문제·설계·진척·남은 일정을 슬라이드로 만든다"),
            ("예상 질문 대비", "심사에서 나올 질문을 뽑아 답을 준비한다"),
        ),
    ),
)

# 요구량이 가용량에 붙거나 넘는 포화 조합 — `horizon_minute_budget` 절단(M19)이 여기서 터진다.
_BUSY_GOALS: tuple[Slots, ...] = (
    Slots(
        key="busy-cram",
        title="전공 기말고사 4과목 대비",
        category="study",
        success_image="네 과목 모두 기출과 요약본을 한 번씩 도는 것",
        current_level="수업은 들었지만 정리가 하나도 안 됐어요.",
        deadline_offset_days=10,
        session_length_min=120,
        weekly_hours=20,
        frequency_per_week=7,
    ),
    Slots(
        key="busy-sprint",
        title="외주 프로젝트 1차 납품",
        category="project",
        success_image="합의한 기능 목록을 다 채워 납품하는 것",
        current_level="설계만 끝났습니다.",
        deadline_offset_days=14,
        session_length_min=120,
        weekly_hours=20,
        frequency_per_week=6,
        season="방학",
    ),
    Slots(
        key="busy-thin",
        title="매일 영어 문장 암송 습관 만들기",
        category="self_dev",
        success_image="하루도 빠짐없이 문장 하나를 외우는 것",
        current_level="계속 작심삼일이었어요.",
        deadline_offset_days=28,
        session_length_min=None,
        weekly_hours=1,
        frequency_per_week=7,
        focus_duration_min=50,
    ),
    Slots(
        key="busy-overflow",
        title="자격증 2개 동시 준비",
        category="career",
        success_image="두 시험 모두 같은 달에 응시해 합격하는 것",
        current_level="둘 다 교재 첫 장입니다.",
        deadline_offset_days=45,
        session_length_min=90,
        weekly_hours=18,
        frequency_per_week=6,
    ),
)


# ── verify 블록의 기준 계획 (손으로 쓴 원안 + 그 슬롯) ────────────────────


class ControlSpec(NamedTuple):
    """무결함 대조군 한 건: 슬롯 + 원안 + 이 케이스가 덮는 회귀 속성."""

    key: str
    slots: Slots
    steps: list[Step]
    properties: tuple[str, ...]
    notes: str


def _control_specs() -> list[ControlSpec]:
    return [
        # ① 세션 길이 == focus_capacity — 120분 사고 회귀. v3 1번 항목이 반려하던 바로 그 형태.
        ControlSpec(
            key="control-capacity-exact",
            slots=Slots(
                key="control-capacity-exact",
                title="졸업논문 1차 초안 제출",
                category="study",
                success_image="서론부터 방법까지 초안을 지도교수님께 보내는 것",
                current_level="주제만 정했어요.",
                deadline_offset_days=28,
                session_length_min=120,
                weekly_hours=8,
                frequency_per_week=4,
                season="방학",
            ),
            steps=[
                Step("자료 정리", "선행연구 10편 표로 정리", 120, "논문 폴더 열고 표 양식 만들기"),
                Step(
                    "자료 정리", "연구 질문 한 문장으로 다듬기", 120, "지난 메모에서 질문 문장 찾기"
                ),
                Step("초안 작성", "서론 초안 작성", 120, "빈 문서에 소제목 3개만 적기"),
                Step("초안 작성", "연구 방법 절 작성", 120, "실험 설계 메모 다시 읽기"),
                Step("초안 작성", "초안 전체 다듬고 보내기", 120, "처음부터 소리 내 읽기"),
            ],
            properties=("session_equals_capacity",),
            notes="세션 길이가 사용자 상한과 정확히 같다 — 반려하면 120분 사고 재현",
        ),
        # ② planned_session_min_for < 15 — §1.1 표의 90건 계열. 10~12분 카드가 정상이다.
        ControlSpec(
            key="control-sub15-normal",
            slots=Slots(
                key="control-sub15-normal",
                title="매일 영어 문장 암송 습관 만들기",
                category="self_dev",
                success_image="하루도 빠짐없이 문장 하나를 외우는 것",
                current_level="계속 작심삼일이었어요.",
                deadline_offset_days=28,
                session_length_min=None,
                weekly_hours=1,
                frequency_per_week=7,
            ),
            steps=[
                Step("문장 고르기", "이번 주 문장 7개 골라 적기", 12, "메모앱에 문장 목록 열기"),
                Step("암송", "월요일 문장 소리 내 외우기", 10, "문장 카드 첫 장 펴기"),
                Step("암송", "화요일 문장 소리 내 외우기", 10, "문장 카드 둘째 장 펴기"),
                Step("점검", "주말에 일곱 문장 이어서 말해보기", 12, "녹음 버튼 켜기"),
            ],
            properties=("sub_15_is_normal",),
            notes="주 1시간 ÷ 주 7회 = 9분 — 룰이 일부러 하한을 낮춘 조합",
        ),
        # ③ `N회차` 연속 세션 — D1 중복 오탐 회귀. 원안을 짧게 줘 룰이 채우게 만든다.
        ControlSpec(
            key="control-repeat-sessions",
            slots=Slots(
                key="control-repeat-sessions",
                title="10km 논스톱으로 달리기",
                category="health",
                success_image="쉬지 않고 10km를 완주하는 것",
                current_level="3km부터 숨이 찹니다.",
                deadline_offset_days=56,
                session_length_min=45,
                weekly_hours=3,
                frequency_per_week=3,
            ),
            steps=[
                Step("기초 체력", "3km 천천히 달리기", 45, "운동화 신고 현관 나서기"),
                Step("기초 체력", "인터벌 5세트 하기", 45, "스톱워치 앱 열기"),
            ],
            properties=("has_repeat_sessions",),
            notes="원안이 2세션뿐이라 룰이 마감까지 회차 세션으로 채운다 — 제목이 거의 같다",
        ),
        # ④ 길이 제각각 — 15분 확인 작업 + 긴 초안. v3 가 '들쭉날쭉'으로 반려하던 정상 편차.
        ControlSpec(
            key="control-mixed-lengths",
            slots=Slots(
                key="control-mixed-lengths",
                title="교환학생 서류 제출 완료",
                category="career",
                success_image="마감 전에 지원 포털에 모든 서류를 올리는 것",
                current_level="어학성적만 있고 나머지는 아직입니다.",
                deadline_offset_days=30,
                session_length_min=90,
                weekly_hours=6,
                frequency_per_week=3,
            ),
            steps=[
                Step("서류 준비", "지원 요강 읽고 필요한 서류 목록 만들기", 45, "요강 PDF 열기"),
                Step("서류 준비", "성적증명서 발급 신청하기", 15, "학사포털 로그인하기"),
                Step("에세이", "지원 동기 에세이 초안 쓰기", 90, "빈 문서에 문단 3개 제목 적기"),
                Step("에세이", "에세이 교수님 피드백 반영하기", 60, "피드백 메일 다시 읽기"),
                Step("제출", "포털에 파일 올리고 제출 확인하기", 15, "포털 지원 탭 열기"),
            ],
            properties=("mixed_lengths",),
            notes="15분 확인 작업과 90분 초안이 한 계획에 공존 — 정상",
        ),
        # ⑤~⑫ 도메인·슬롯을 흩어 M29 의 분모를 넓힌다. 12건이면 오탐 1건 = 0.083 으로
        # 사전 고정 임계값(<= 0.10)을 그나마 분해할 수 있다. 6건이면 1건이 0.167 이라 못 잰다.
        ControlSpec(
            key="control-cert-standard",
            slots=Slots(
                key="control-cert-standard",
                title="정보처리기사 실기 합격",
                category="career",
                success_image="실기 시험에서 60점을 넘겨 합격증을 받는 것",
                current_level="필기는 붙었고 실기는 아직입니다.",
                deadline_offset_days=35,
                session_length_min=50,
                weekly_hours=6,
                frequency_per_week=3,
            ),
            steps=[
                Step("개념", "SQL 활용 단원 개념 정리하기", 50, "교재 SQL 챕터 펴기"),
                Step("개념", "프로그래밍 언어 활용 개념 정리하기", 50, "교재 언어 챕터 펴기"),
                Step("기출", "SQL 활용 기출 3회차 풀기", 50, "기출 PDF 3회차 열기"),
                Step("기출", "틀린 문제만 다시 풀기", 50, "오답 노트 열기"),
            ],
            properties=(),
            notes="가장 흔한 형태 — 여기서 반려가 나오면 검토기 전반이 의심된다",
        ),
        ControlSpec(
            key="control-toeic-short",
            slots=Slots(
                key="control-toeic-short",
                title="토익 800점 넘기기",
                category="study",
                success_image="다음 정기시험에서 800점 이상 받는 것",
                current_level="작년에 690점 받고 손 놨어요.",
                deadline_offset_days=28,
                session_length_min=30,
                weekly_hours=5,
                frequency_per_week=5,
            ),
            steps=[
                Step("LC", "Part 3 대화 20문항 풀기", 30, "음원 파일 재생하기"),
                Step("LC", "받아쓰기로 안 들린 문장 확인하기", 30, "받아쓰기 노트 펴기"),
                Step("RC", "Part 5 문법 30문항 풀기", 30, "문제집 Part 5 펴기"),
                Step("RC", "Part 7 지문 3개 정독하기", 30, "지문 첫 줄 읽기"),
            ],
            properties=(),
            notes="짧은 세션이 균일한 정상 계획",
        ),
        ControlSpec(
            key="control-portfolio-long",
            slots=Slots(
                key="control-portfolio-long",
                title="개발 포트폴리오 사이트 완성",
                category="project",
                success_image="프로젝트 3개가 정리된 사이트를 배포하는 것",
                current_level="리액트로 화면 몇 개 만들어봤어요.",
                deadline_offset_days=42,
                session_length_min=120,
                weekly_hours=8,
                frequency_per_week=2,
                approach_note="완성도보다 개수를 우선하고 싶어요.",
            ),
            steps=[
                Step("구조", "페이지 구조 정하고 라우팅 잡기", 120, "새 프로젝트 폴더 만들기"),
                Step("내용", "프로젝트 1 소개 글과 스크린샷 넣기", 120, "예전 저장소 README 열기"),
                Step("내용", "프로젝트 2 소개 글과 스크린샷 넣기", 120, "두 번째 저장소 열기"),
                Step("배포", "배포하고 링크 확인하기", 120, "배포 설정 파일 열기"),
            ],
            properties=(),
            notes="긴 세션이 균일한 정상 계획",
        ),
        ControlSpec(
            key="control-guitar-freq-only",
            slots=Slots(
                key="control-guitar-freq-only",
                title="기타로 좋아하는 곡 한 곡 완주",
                category="self_dev",
                success_image="코드 안 보고 한 곡을 끝까지 치는 것",
                current_level="코드 4개 정도 잡을 줄 압니다.",
                deadline_offset_days=35,
                session_length_min=25,
                weekly_hours=None,
                frequency_per_week=5,
            ),
            steps=[
                Step("코드", "F 코드 바레 잡는 연습하기", 25, "기타 꺼내 F 코드 짚기"),
                Step("코드", "코드 전환 4개 반복하기", 25, "메트로놈 60에 맞추기"),
                Step("곡", "1절까지 악보 보고 쳐보기", 25, "악보 첫 마디 펴기"),
            ],
            properties=(),
            notes="주당 시간 미답 — `planned_session_min_for` 가 용량을 그대로 쓰는 경로",
        ),
        ControlSpec(
            key="control-thesis-branchy",
            slots=Slots(
                key="control-thesis-branchy",
                title="학부생 학술대회 포스터 발표",
                category="study",
                success_image="포스터를 인쇄해 발표장에 서는 것",
                current_level="실험 데이터는 다 모았고 정리가 안 됐어요.",
                deadline_offset_days=49,
                session_length_min=90,
                weekly_hours=6,
                frequency_per_week=2,
                season="방학",
            ),
            steps=[
                Step("분석", "측정 데이터 표로 정리하기", 90, "원본 CSV 파일 열기"),
                Step("분석", "그래프 3종 그려보고 하나 고르기", 90, "플롯 스크립트 열기"),
                Step("포스터", "포스터 레이아웃 초안 잡기", 90, "템플릿 파일 내려받기"),
                Step("포스터", "본문 문구 줄이고 그림 배치하기", 60, "초안 파일 열기"),
                Step("발표", "3분 발표 대본 소리 내 읽기", 30, "대본 첫 문단 읽기"),
            ],
            properties=("mixed_lengths",),
            notes="branch 3개 + 길이 편차 — 순서가 실제로 의존적인 정상 계획(D3 대조)",
        ),
        ControlSpec(
            key="control-health-routine",
            slots=Slots(
                key="control-health-routine",
                title="주 3회 근력운동 루틴 정착",
                category="health",
                success_image="8주 동안 주 3회를 빠짐없이 채우는 것",
                current_level="헬스장은 등록만 해뒀어요.",
                deadline_offset_days=56,
                session_length_min=60,
                weekly_hours=3,
                frequency_per_week=3,
            ),
            steps=[
                Step("하체", "스쿼트 폼 영상 보고 맨몸으로 연습하기", 60, "운동복 갈아입기"),
                Step("상체", "푸시업과 랫풀다운 하기", 60, "헬스장 가방 챙기기"),
                Step("전신", "기구 3종 한 바퀴 돌기", 60, "운동 기록 앱 열기"),
            ],
            properties=("has_repeat_sessions",),
            notes="원안 3세션 + 8주 지평 — 회차 세션이 대량으로 붙는다",
        ),
        ControlSpec(
            key="control-reading-plan",
            slots=Slots(
                key="control-reading-plan",
                title="통계학 입문서 완독",
                category="study",
                success_image="책 한 권을 끝까지 읽고 요약 노트를 남기는 것",
                current_level="1장만 읽고 덮었어요.",
                deadline_offset_days=70,
                session_length_min=45,
                weekly_hours=3,
                frequency_per_week=3,
            ),
            steps=[
                Step("읽기", "2장 기술통계 읽고 예제 따라 풀기", 45, "책 2장 펴기"),
                Step("읽기", "3장 확률 읽고 예제 따라 풀기", 45, "책 3장 펴기"),
                Step("읽기", "4장 추정 읽고 예제 따라 풀기", 45, "책 4장 펴기"),
                Step("정리", "장별 요약 노트 한 쪽으로 줄이기", 45, "요약 노트 파일 열기"),
            ],
            properties=(),
            notes="자료 목차를 따르는 순서 — D3 정상 앵커에 해당",
        ),
        # ⑫ 손으로 쓴 내용이 채움 세션보다 많은 케이스. 나머지 대조군은 원안이 짧아
        # `N회차` 채움이 과반인데, 실제 분해는 최대 20세션까지 내므로 그 반대편도 있어야
        # 한다 — 채움이 과반인 계획만으로 재면 D1 오탐률이 실제보다 높게 나온다.
        # ⚠️ 원안 10개 중 **9개만 남는다.** 분 예산(810분)에는 45분이 남는데 케이던스
        # 상한(주 3회 × 3주 = 9세션)이 **먼저** 걸려 마지막 항목이 잘린다. 절단이 분이
        # 아니라 개수로 일어나는 경로가 실재한다는 뜻이라, 이 케이스는 그 자체로 M19
        # (절단율)의 관찰 지점이기도 하다.
        ControlSpec(
            key="control-dense-plan",
            slots=Slots(
                key="control-dense-plan",
                title="교내 창업경진대회 본선 진출",
                category="project",
                success_image="마감일까지 사업계획서와 발표자료를 제출하는 것",
                current_level="아이디어만 있고 문서는 없습니다.",
                deadline_offset_days=21,
                session_length_min=90,
                weekly_hours=9,
                frequency_per_week=3,
            ),
            steps=[
                Step("조사", "경쟁 서비스 5개 기능 비교표 만들기", 90, "비교표 스프레드시트 열기"),
                Step("조사", "타깃 사용자 5명 인터뷰 질문 만들기", 60, "질문 초안 문서 열기"),
                Step("조사", "인터뷰 답변 정리해 문제 정의 한 문단 쓰기", 90, "녹취 파일 열기"),
                Step("문서", "사업계획서 목차 잡기", 45, "요강의 심사 기준 다시 읽기"),
                Step("문서", "문제·해결 파트 작성하기", 90, "문제 정의 문단 복사해 붙이기"),
                Step("문서", "시장 규모와 수익 모델 작성하기", 90, "조사 자료 폴더 열기"),
                Step("문서", "사업계획서 전체 다듬고 분량 맞추기", 90, "처음부터 소리 내 읽기"),
                Step("발표", "발표 슬라이드 10장 구성 잡기", 60, "슬라이드 새 파일 만들기"),
                Step("발표", "슬라이드에 도표 넣기", 90, "비교표 캡처하기"),
                Step("발표", "5분 발표 연습하고 시간 재기", 45, "타이머 5분 맞추기"),
            ],
            properties=("mixed_lengths",),
            notes="원안 10세션 중 9개가 남고 채움 세션은 0개 — 룰 채움이 없는 대조군",
        ),
        # ── 도메인 확장 대조군 (2026-09-02) — M29 표본을 30건 위로 올린다.
        # 사유: 12건에서는 0건 반려여도 95% 상한이 25%(rule of three: 3/n)라
        # 사전등록의 절대 임계값 **≤0.10 을 확인했다고 말할 수 없다**. 30건이면
        # 0건 반려일 때 상한이 10% 로 내려온다. 반복 3회는 같은 케이스라
        # 상관이 있어 독립 표본으로 세면 안 된다.
        ControlSpec(
            key="ctl-cs-algo",
            slots=Slots(
                key="ctl-cs-algo",
                title="알고리즘 문제풀이 실력 올리기",
                category="study",
                success_image="코딩테스트 중급 문제를 시간 안에 푸는 것",
                current_level="쉬운 문제만 겨우 풉니다.",
                deadline_offset_days=35,
                session_length_min=60,
                weekly_hours=4,
                frequency_per_week=3,
                season="학기 중",
            ),
            steps=[
                Step("기초 다지기", "배열·문자열 문제 3개 풀기", 60, "문제집 사이트 로그인하기"),
                Step("기초 다지기", "정렬 문제 3개 풀기", 60, "정렬 카테고리 열기"),
                Step("응용", "완전탐색 문제 2개 풀기", 60, "어제 푼 코드 다시 열기"),
                Step("응용", "틀린 문제 다시 풀고 노트 정리", 60, "오답 노트 파일 열기"),
            ],
            properties=(),
            notes="도메인 확장 — study 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-design-ui",
            slots=Slots(
                key="ctl-design-ui",
                title="포트폴리오용 UI 리디자인",
                category="project",
                success_image="화면 3개를 새 디자인으로 다시 그리는 것",
                current_level="예전 시안만 있어요.",
                deadline_offset_days=28,
                session_length_min=90,
                weekly_hours=6,
                frequency_per_week=2,
                season="학기 중",
            ),
            steps=[
                Step("리서치", "참고 서비스 3개 화면 캡처 정리", 90, "캡처 폴더 만들기"),
                Step("리서치", "색·타이포 규칙 한 장으로 정하기", 90, "피그마 새 파일 열기"),
                Step("제작", "홈 화면 시안 그리기", 90, "홈 프레임 만들기"),
                Step("제작", "상세 화면 시안 그리기", 90, "홈 시안 복제하기"),
            ],
            properties=(),
            notes="도메인 확장 — project 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-health-run",
            slots=Slots(
                key="ctl-health-run",
                title="5km 쉬지 않고 달리기",
                category="health",
                success_image="한 번도 안 걷고 5km 를 완주하는 것",
                current_level="2km 에서 숨이 찹니다.",
                deadline_offset_days=42,
                session_length_min=40,
                weekly_hours=2,
                frequency_per_week=3,
                season="학기 중",
            ),
            steps=[
                Step("지구력", "2km 천천히 달리기", 40, "러닝화 신고 현관 나가기"),
                Step("지구력", "3km 목표로 늘려 달리기", 40, "달리기 앱 켜기"),
                Step("회복", "가볍게 걷고 스트레칭", 40, "매트 펴기"),
                Step("점검", "5km 시도해보고 기록 남기기", 40, "기록 앱 새 세션 만들기"),
            ],
            properties=(),
            notes="도메인 확장 — health 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-lang-jp",
            slots=Slots(
                key="ctl-lang-jp",
                title="일본어 JLPT N3 합격",
                category="study",
                success_image="시험에서 합격 점수를 받는 것",
                current_level="히라가나는 읽어요.",
                deadline_offset_days=56,
                session_length_min=50,
                weekly_hours=5,
                frequency_per_week=4,
                season="학기 중",
            ),
            steps=[
                Step("문법", "N3 문법 1~10과 정리", 50, "교재 1권 펴기"),
                Step("문법", "N3 문법 11~20과 정리", 50, "지난 정리 노트 열기"),
                Step("어휘", "빈출 단어 300개 1회독", 50, "단어장 앱 열기"),
                Step("실전", "기출 1회분 시간 재고 풀기", 50, "타이머 맞추기"),
            ],
            properties=(),
            notes="도메인 확장 — study 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-writing-blog",
            slots=Slots(
                key="ctl-writing-blog",
                title="기술 블로그 글 4편 발행",
                category="project",
                success_image="네 편을 실제로 게시하는 것",
                current_level="초안만 두 개 있습니다.",
                deadline_offset_days=28,
                session_length_min=60,
                weekly_hours=4,
                frequency_per_week=2,
                season="학기 중",
            ),
            steps=[
                Step("주제", "쓸 주제 4개 정하고 개요 잡기", 60, "메모앱에 목록 만들기"),
                Step("작성", "1편 초안 쓰기", 60, "개요 문서 복제하기"),
                Step("작성", "2편 초안 쓰기", 60, "1편 초안 다시 읽기"),
                Step("발행", "교정하고 게시하기", 60, "블로그 관리자 열기"),
            ],
            properties=(),
            notes="도메인 확장 — project 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-music-piano",
            slots=Slots(
                key="ctl-music-piano",
                title="피아노 곡 한 곡 완주",
                category="self_dev",
                success_image="악보 없이 한 곡을 끝까지 치는 것",
                current_level="앞부분만 됩니다.",
                deadline_offset_days=42,
                session_length_min=30,
                weekly_hours=2,
                frequency_per_week=4,
                season="학기 중",
            ),
            steps=[
                Step("연습", "A 파트 마디별로 천천히 치기", 30, "악보 첫 장 펴기"),
                Step("연습", "B 파트 마디별로 천천히 치기", 30, "악보 둘째 장 펴기"),
                Step("연결", "A-B 이어서 치기", 30, "메트로놈 켜기"),
                Step("마무리", "전곡 녹음해서 들어보기", 30, "녹음 앱 켜기"),
            ],
            properties=(),
            notes="도메인 확장 — self_dev 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-finance-budget",
            slots=Slots(
                key="ctl-finance-budget",
                title="가계부 3개월 유지",
                category="routine",
                success_image="매주 빠짐없이 기록이 남는 것",
                current_level="몇 번 쓰다 말았어요.",
                deadline_offset_days=63,
                session_length_min=20,
                weekly_hours=2,
                frequency_per_week=5,
                season="학기 중",
            ),
            steps=[
                Step("설정", "카테고리 정하고 앱 세팅", 20, "가계부 앱 설치하기"),
                Step("기록", "이번 주 지출 입력하기", 20, "영수증 사진 폴더 열기"),
                Step("기록", "다음 주 지출 입력하기", 20, "앱 홈 열기"),
                Step("점검", "한 달 지출 요약 보기", 20, "월간 리포트 탭 누르기"),
            ],
            properties=(),
            notes="도메인 확장 — routine 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-career-resume",
            slots=Slots(
                key="ctl-career-resume",
                title="이력서와 포트폴리오 정리",
                category="career",
                success_image="지원 가능한 상태의 문서 세트를 갖는 것",
                current_level="예전 버전만 있어요.",
                deadline_offset_days=21,
                session_length_min=45,
                weekly_hours=3,
                frequency_per_week=3,
                season="학기 중",
            ),
            steps=[
                Step("정리", "경력·프로젝트 목록 뽑기", 45, "예전 이력서 파일 열기"),
                Step("작성", "이력서 본문 다시 쓰기", 45, "새 문서 만들기"),
                Step("작성", "프로젝트 설명 3개 다듬기", 45, "프로젝트 폴더 열기"),
                Step("점검", "오탈자 확인하고 PDF 내보내기", 45, "맞춤법 검사 실행하기"),
            ],
            properties=(),
            notes="도메인 확장 — career 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-research-review",
            slots=Slots(
                key="ctl-research-review",
                title="선행연구 리뷰 정리",
                category="study",
                success_image="리뷰 표가 완성되는 것",
                current_level="논문 몇 편만 읽었어요.",
                deadline_offset_days=35,
                session_length_min=90,
                weekly_hours=6,
                frequency_per_week=2,
                season="방학",
            ),
            steps=[
                Step("수집", "검색어 정하고 논문 20편 모으기", 90, "학술 DB 접속하기"),
                Step("읽기", "10편 초록 읽고 분류하기", 90, "첫 논문 PDF 열기"),
                Step("읽기", "나머지 10편 초록 읽고 분류하기", 90, "분류 표 열기"),
                Step("정리", "리뷰 표 채우고 요약 쓰기", 90, "표 양식 열기"),
            ],
            properties=(),
            notes="도메인 확장 — study 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-cooking",
            slots=Slots(
                key="ctl-cooking",
                title="일주일 도시락 직접 싸기",
                category="routine",
                success_image="평일 닷새 도시락을 싸 가는 것",
                current_level="거의 사 먹습니다.",
                deadline_offset_days=28,
                session_length_min=30,
                weekly_hours=3,
                frequency_per_week=5,
                season="학기 중",
            ),
            steps=[
                Step(
                    "준비", "일주일 메뉴 정하고 장보기 목록 쓰기", 30, "냉장고 열어 재고 확인하기"
                ),
                Step("준비", "주말에 밑반찬 두 가지 만들기", 30, "재료 꺼내 씻기"),
                Step("실행", "평일 도시락 싸기", 30, "도시락통 꺼내기"),
                Step("점검", "남은 재료로 다음 주 메뉴 조정", 30, "메뉴 메모 열기"),
            ],
            properties=(),
            notes="도메인 확장 — routine 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-volunteer",
            slots=Slots(
                key="ctl-volunteer",
                title="봉사활동 30시간 채우기",
                category="relationship",
                success_image="확인서에 30시간이 찍히는 것",
                current_level="8시간만 했어요.",
                deadline_offset_days=49,
                session_length_min=120,
                weekly_hours=4,
                frequency_per_week=2,
                season="학기 중",
            ),
            steps=[
                Step("탐색", "가능한 봉사처 3곳 알아보기", 120, "봉사 포털 접속하기"),
                Step("신청", "일정 맞는 곳에 신청서 내기", 120, "신청 양식 내려받기"),
                Step("활동", "주말 봉사 참여하기", 120, "가방에 확인서 챙기기"),
                Step("정리", "확인서 모아 시간 합산하기", 120, "확인서 폴더 열기"),
            ],
            properties=(),
            notes="도메인 확장 — relationship 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-cert-network",
            slots=Slots(
                key="ctl-cert-network",
                title="네트워크관리사 2급 필기 합격",
                category="career",
                success_image="필기 시험에 합격하는 것",
                current_level="용어부터 낯설어요.",
                deadline_offset_days=35,
                session_length_min=50,
                weekly_hours=5,
                frequency_per_week=4,
                season="학기 중",
            ),
            steps=[
                Step("이론", "TCP/IP 계층 개념 정리", 50, "교재 1장 펴기"),
                Step("이론", "라우팅·스위칭 개념 정리", 50, "지난 정리 노트 열기"),
                Step("문제", "기출 2회분 풀기", 50, "기출 PDF 열기"),
                Step("문제", "틀린 문항 유형별로 묶기", 50, "오답 노트 만들기"),
            ],
            properties=(),
            notes="도메인 확장 — career 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-club-event",
            slots=Slots(
                key="ctl-club-event",
                title="동아리 신입 모집 행사 준비",
                category="project",
                success_image="행사 당일 부스를 여는 것",
                current_level="아이디어 회의만 했어요.",
                deadline_offset_days=21,
                session_length_min=60,
                weekly_hours=6,
                frequency_per_week=3,
                season="학기 중",
            ),
            steps=[
                Step("기획", "행사 구성과 역할 나누기", 60, "회의록 문서 열기"),
                Step("준비", "홍보물 문구 쓰고 인쇄 맡기기", 60, "지난 홍보물 파일 열기"),
                Step("준비", "부스 물품 목록 만들고 챙기기", 60, "창고 열쇠 받기"),
                Step("실행", "부스 설치하고 운영하기", 60, "행사장 도착해 자리 확인하기"),
            ],
            properties=(),
            notes="도메인 확장 — project 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-reading-classic",
            slots=Slots(
                key="ctl-reading-classic",
                title="고전 3권 완독",
                category="self_dev",
                success_image="세 권을 끝까지 읽는 것",
                current_level="한 권 절반쯤 봤어요.",
                deadline_offset_days=56,
                session_length_min=40,
                weekly_hours=3,
                frequency_per_week=4,
                season="방학",
            ),
            steps=[
                Step("1권", "1권 남은 절반 읽기", 40, "책갈피 위치 펴기"),
                Step("2권", "2권 앞부분 읽기", 40, "2권 첫 장 펴기"),
                Step("2권", "2권 뒷부분 읽기", 40, "어제 읽은 곳 찾기"),
                Step("3권", "3권 읽고 짧은 감상 남기기", 40, "메모앱 새 노트 만들기"),
            ],
            properties=(),
            notes="도메인 확장 — self_dev 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-sleep-routine",
            slots=Slots(
                key="ctl-sleep-routine",
                title="취침 시간 일정하게 만들기",
                category="health",
                success_image="2주 내내 같은 시각에 눕는 것",
                current_level="매일 들쭉날쭉합니다.",
                deadline_offset_days=28,
                session_length_min=15,
                weekly_hours=2,
                frequency_per_week=7,
                season="학기 중",
            ),
            steps=[
                Step("환경", "잘 시간 알림 맞추고 침실 정리", 15, "휴대폰 알람 앱 열기"),
                Step("실행", "알림 뜨면 화면 끄고 눕기", 15, "충전기에 휴대폰 꽂기"),
                Step("실행", "다음 날도 같은 시각에 눕기", 15, "조명 스위치 내리기"),
                Step("점검", "일주일 기록 보고 시간 조정", 15, "수면 기록 앱 열기"),
            ],
            properties=(),
            notes="도메인 확장 — health 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-data-viz",
            slots=Slots(
                key="ctl-data-viz",
                title="데이터 시각화 대시보드 만들기",
                category="project",
                success_image="대시보드를 팀에 공유하는 것",
                current_level="데이터만 모아 뒀어요.",
                deadline_offset_days=28,
                session_length_min=90,
                weekly_hours=6,
                frequency_per_week=2,
                season="학기 중",
            ),
            steps=[
                Step("정리", "원본 데이터 결측치 정리", 90, "데이터 파일 열기"),
                Step("설계", "보여줄 지표 3개 정하기", 90, "빈 문서에 지표 후보 적기"),
                Step("제작", "차트 3개 그리기", 90, "시각화 도구 새 프로젝트 만들기"),
                Step("공유", "대시보드 링크 팀에 보내기", 90, "공유 설정 열기"),
            ],
            properties=(),
            notes="도메인 확장 — project 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-speech",
            slots=Slots(
                key="ctl-speech",
                title="발표 스크립트 없이 말하기",
                category="self_dev",
                success_image="10분 발표를 대본 없이 마치는 것",
                current_level="대본을 읽게 됩니다.",
                deadline_offset_days=21,
                session_length_min=30,
                weekly_hours=3,
                frequency_per_week=4,
                season="학기 중",
            ),
            steps=[
                Step("구조", "발표 흐름 5단계로 쪼개기", 30, "빈 종이에 단계 적기"),
                Step("연습", "단계별로 소리 내 말해보기", 30, "타이머 켜기"),
                Step("연습", "이어서 전체 말해보기", 30, "거울 앞에 서기"),
                Step("점검", "녹화해서 늘어지는 곳 찾기", 30, "카메라 세우기"),
            ],
            properties=(),
            notes="도메인 확장 — self_dev 계열 무결함 계획",
        ),
        ControlSpec(
            key="ctl-git-oss",
            slots=Slots(
                key="ctl-git-oss",
                title="오픈소스에 PR 한 건 머지",
                category="project",
                success_image="PR 이 실제로 머지되는 것",
                current_level="이슈만 둘러봤어요.",
                deadline_offset_days=42,
                session_length_min=60,
                weekly_hours=4,
                frequency_per_week=3,
                season="학기 중",
            ),
            steps=[
                Step("탐색", "good first issue 3개 추려 읽기", 60, "저장소 이슈 탭 열기"),
                Step("환경", "로컬에 빌드하고 테스트 돌리기", 60, "저장소 클론하기"),
                Step("작업", "고른 이슈 수정하고 테스트 추가", 60, "브랜치 만들기"),
                Step("제출", "PR 올리고 리뷰 반영하기", 60, "PR 양식 열기"),
            ],
            properties=(),
            notes="도메인 확장 — project 계열 무결함 계획",
        ),
    ]


def base_plans() -> dict[str, tuple[Slots, GoalDecomposition]]:
    """대조군 명세를 ③층에 태운 결과 — `verify` 블록의 계획 원천.

    테스트가 이 함수로 기준 계획을 다시 만들어, 심은 결함이 **실제로 계획을 바꿨는지**
    (뮤테이션 가드) 확인한다.
    """
    out: dict[str, tuple[Slots, GoalDecomposition]] = {}
    for spec in _control_specs():
        outcome = _outcome(spec.slots)
        raw = _raw_plan(spec.steps, goal_title=spec.slots.title, category=spec.slots.category)
        out[spec.key] = (spec.slots, _shaped(outcome, raw))
    return out


# `seeded_defect` 가 결함을 심을 기준 계획 2개. 학습형 / 프로젝트형으로 도메인을 갈라
# 결함 탐지가 특정 도메인 문구에만 붙는지 본다.
SEED_BASE_KEYS = ("control-cert-standard", "control-portfolio-long")


# ── 결함 주입 연산 (held-out 파일이 지정하는 op 을 결정적으로 적용) ────────


def _apply_operation(plan: GoalDecomposition, op: dict[str, Any]) -> GoalDecomposition:
    """held-out 결함 파일의 `operation` 을 계획에 적용한다.

    op 어휘는 4종뿐이다. 어휘를 좁게 둔 이유: 결함 **내용**은 다른 모델이 자유롭게 쓰되,
    **적용 방식**은 이 코드가 결정적으로 재현해야 하기 때문이다. 자유 형식 계획을 통째로
    받으면 무엇이 결함이고 무엇이 기준 계획인지 diff 로 확인할 수 없다.
    """
    nodes = [n.model_copy(deep=True) for n in plan.goal_nodes]
    items = [a.model_copy(deep=True) for a in plan.action_items]
    kind = op["op"]

    if kind == "replace_title":
        target = op["node_id"]
        for n in nodes:
            if n.node_id == target:
                n.title = op["value"]
        for a in items:
            if a.node_id == target:
                a.title = op["value"]

    elif kind == "replace_first_step":
        target = op["node_id"]
        for a in items:
            if a.node_id == target:
                a.first_step = op["value"]

    elif kind == "swap_order":
        first, second = op["node_ids"]
        idx = {a.node_id: i for i, a in enumerate(items)}
        i, j = idx[first], idx[second]
        items[i], items[j] = items[j], items[i]
        node_idx = {n.node_id: k for k, n in enumerate(nodes)}
        ni, nj = node_idx[first], node_idx[second]
        nodes[ni].order_index, nodes[nj].order_index = (
            nodes[nj].order_index,
            nodes[ni].order_index,
        )
        nodes[ni], nodes[nj] = nodes[nj], nodes[ni]

    elif kind == "insert_item":
        after = op["after"]
        anchor = next(a for a in items if a.node_id == after)
        anchor_node = next(n for n in nodes if n.node_id == after)
        new_id = op["node_id"]
        slot = anchor_node.order_index + 1
        # 뒤 형제를 한 칸씩 민다. 이걸 안 하면 삽입 지점이 branch **중간**일 때 같은 부모
        # 아래 `order_index` 가 중복된다. 2026-09-01 감사에서 4건(d1-easy-cert ·
        # d1-boundary-cert · d1-easy-portfolio · d5-boundary-cert)이 실제로 중복이었다 —
        # 삽입 지점이 branch 끝이면 우연히 피해가서 데이터에 따라 나타났다 사라졌다 하는
        # 잠재 결함이었다.
        #
        # ⚠️ **근거 정정 (2026-09-02 감사).** 이 자리에 원래 "③층이 enumerate 로 매기므로
        # 프로덕션 트리에는 중복이 없다" 고 썼는데 **사실이 아니다.** 계획 트리의
        # `order_index` 는 LLM 초안 값을 그대로 복사한다(`first_plan_adapter.py`
        # `n.order_index = nd.order_index`). enumerate 로 매기는 자리는 셋뿐이다 —
        # `extend_action_plan_to_horizon` 의 채움 leaf(와 그 branch 는 `len(nodes)`),
        # 마일스톤 트리(depth 1), 그리고 `first_plan.py` 의 **룰 폴백 계획 빌더**(분해가
        # 타임아웃했을 때 프로덕션에서 실제로 도는 경로). 정상 LLM 경로는 어디에도 없다 —
        # 즉 프로덕션도 원리적으로 중복을 낼 수 있다.
        #
        # 그래도 미는 쪽을 유지하는 이유는 다르다: 이 골든셋의 기준 계획은 `_raw_plan` 이
        # branch 안에서 enumerate 로 만든 것이라 **중복이 없다**. 주입이 그 성질을 깨면
        # 결함 작성자가 의도하지 않은 두 번째 변화가 계획에 섞여, 검토기의 반려가 심은
        # 결함 때문인지 인덱스 중복 때문인지 못 가른다. 미는 것은 골든셋을 **좁히는**
        # 선택이지 프로덕션 보장을 재현하는 것이 아니다.
        for n in nodes:
            if n.parent_id == anchor_node.parent_id and n.order_index >= slot:
                n.order_index += 1
        nodes.insert(
            nodes.index(anchor_node) + 1,
            GoalNodeDraft(
                node_id=new_id,
                parent_id=anchor_node.parent_id,
                title=op["title"],
                node_type="leaf",
                order_index=slot,
                is_leaf=True,
            ),
        )
        items.insert(
            items.index(anchor) + 1,
            ActionItemDraft(
                node_id=new_id,
                title=op["title"],
                estimated_minutes=op.get("estimated_minutes", anchor.estimated_minutes),
                category=op.get("category", anchor.category),
                first_step=op["first_step"],
            ),
        )

    else:  # pragma: no cover - 테스트가 어휘를 고정한다
        raise ValueError(f"알 수 없는 결함 주입 연산: {kind}")

    return GoalDecomposition(goal_nodes=nodes, action_items=items, policy_violations=[])


def load_seeded_defects() -> dict[str, Any]:
    """held-out 결함 파일을 읽는다 — 이 생성기는 내용을 만들지 않는다."""
    return json.loads(DEFECTS_PATH.read_text(encoding="utf-8"))


# ── 케이스 조립 ───────────────────────────────────────────────────────────


def _decompose_case(
    *, case_id: str, block: str, slots: Slots, notes: str, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    case = {
        "case_id": case_id,
        "block": block,
        "kind": "decompose",
        "synthetic": True,
        "interview": _interview_payload(slots),
        "notes": notes,
    }
    if extra:
        case.update(extra)
    return case


def _verify_case(
    *,
    case_id: str,
    block: str,
    slots: Slots,
    plan: GoalDecomposition,
    notes: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    case = {
        "case_id": case_id,
        "block": block,
        "kind": "verify",
        "synthetic": True,
        "interview": _interview_payload(slots),
        "plan": _plan_payload(plan),
        "notes": notes,
    }
    if extra:
        case.update(extra)
    return case


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    # ── normal — 6목표 × 마감 2종
    for goal in _NORMAL_GOALS:
        for offset, tag in _NORMAL_HORIZONS:
            slots = goal._replace(key=f"{goal.key}-{tag}", deadline_offset_days=offset)
            cases.append(
                _decompose_case(
                    case_id=slots.key,
                    block="normal",
                    slots=slots,
                    notes=f"기준선 — 마감 {offset}일",
                )
            )

    # ── constraint_edge — 집중 용량 ±5분 격자
    for anchor in _EDGE_ANCHORS:
        for offset in _EDGE_OFFSETS:
            length = anchor + offset
            slots = _EDGE_BASE._replace(key=f"edge-{anchor}{offset:+d}", session_length_min=length)
            cases.append(
                _decompose_case(
                    case_id=slots.key,
                    block="constraint_edge",
                    slots=slots,
                    notes=f"용량 {anchor}분 기준 {offset:+d}분 — 반려율 곡선의 격자점",
                    extra={"edge": {"anchor_min": anchor, "offset_min": offset}},
                )
            )

    # ── milestone_fixed
    for goal in _MILESTONE_GOALS:
        cases.append(
            _decompose_case(
                case_id=goal.key,
                block="milestone_fixed",
                slots=goal,
                notes="외부에서 날짜가 고정된 목표 — 지평 커버리지·배치",
            )
        )

    # ── busy_saturated
    for goal in _BUSY_GOALS:
        cases.append(
            _decompose_case(
                case_id=goal.key,
                block="busy_saturated",
                slots=goal,
                notes="요구량이 가용량에 붙거나 넘는 조합 — 분량 절단",
            )
        )

    # ── defect_free_control — 원안을 ③층에 태운 결과가 곧 케이스의 계획이다
    shaped_by_key = base_plans()
    for spec in _control_specs():
        _, plan = shaped_by_key[spec.key]
        cases.append(
            _verify_case(
                case_id=spec.key,
                block="defect_free_control",
                slots=spec.slots,
                plan=plan,
                notes=spec.notes,
                extra={
                    "expected": {"approved": True},
                    "control_properties": list(spec.properties),
                },
            )
        )

    # ── seeded_defect — 내용은 held-out 파일, 적용은 여기
    seeded = load_seeded_defects()
    for entry in seeded["defects"]:
        slots, base_plan = shaped_by_key[entry["base_plan"]]
        # ⚠️ 주입 **뒤에 ③층을 다시 태운다.** 결함 파일은 기준 계획(=이미 ③층을 통과한 것)의
        # node_id 를 보고 쓰였으므로 주입은 그 계획에 해야 하는데, `insert_item` 은 카드를
        # 늘리므로 그대로 두면 총량 상한 두 개(`cadence_session_cap` 개수 · `_take_within_budget`
        # 분 예산)를 넘긴 계획이 나온다. 그건 ③층이 절대 내보내지 않는 형태라 "이 계획은 실제
        # ③층 산출물" 이라는 골든셋의 전제가 깨진다 — 2026-09-02 감사에서 20건 중 8건
        # (d1·d5 의 easy/boundary × cert/portfolio)이 실제로 그 상태였다.
        #
        # 재적용이 결함을 지우지는 않는다: `_take_within_budget` 이 앞에서부터 담으므로
        # branch 중간에 삽입된 카드는 남고 **꼬리의 채움 카드**(`tmp-continue-N`)가 밀려난다.
        # 20건 전부 `target_node_ids` 가 생존하는 것을 확인했고, 그 성질은
        # `test_seeded_target_nodes_exist_in_the_plan` 이 계속 지킨다.
        #
        # 항목별 분 상한만 보는 테스트로는 이 결함이 안 잡힌다(총량은 항목별 상한을 하나도
        # 안 넘기면서 초과할 수 있다) — `test_seeded_defects_respect_layer3_volume_caps` 가
        # 개수·분 예산을 따로 핀한다.
        mutated = _shaped(_outcome(slots), _apply_operation(base_plan, entry["operation"]))
        # 주입 지점을 지운다 — 아래 `_renumber_leaves` 주석 참조. 정답 키(`target_node_ids`)도
        # 같은 매핑으로 옮긴다. 매핑에 없는 id(`tmp-continue-*`)는 그대로 둔다.
        mutated, id_map = _renumber_leaves(mutated)
        target_node_ids = [id_map.get(n, n) for n in entry["target_node_ids"]]
        cases.append(
            _verify_case(
                case_id=entry["defect_id"],
                block="seeded_defect",
                slots=slots,
                plan=mutated,
                notes=entry["rationale"],
                extra={
                    "seeded": {
                        "base_plan": entry["base_plan"],
                        "defect": entry["defect"],
                        "level": entry["level"],
                        "target_node_ids": target_node_ids,
                        "operation": entry["operation"],
                    },
                    # boundary 는 §2 의 **경계** 앵커다 — 반려하면 오탐, 통과가 정답.
                    "expected": {"approved": entry["level"] == "boundary"},
                    "author": seeded["provenance"]["author_model"],
                },
            )
        )

    return cases


def to_jsonl(cases: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n" for c in cases)


def dump_base_plans() -> str:
    """held-out 결함 설계자에게 넘길 기준 계획 JSON.

    ⚠️ 여기서 나가는 것은 **기준 계획과 op 어휘뿐**이다. 루브릭 §2 의 앵커 표(정상/경계/결함
    예시)와 §3 의 주입 레시피는 넘기지 않는다 — 넘기는 순간 held-out 이 아니게 된다.
    """
    payload = []
    plans = base_plans()
    for spec in _control_specs():
        if spec.key not in SEED_BASE_KEYS:
            continue
        slots, plan = plans[spec.key]
        payload.append(
            {
                "base_plan": spec.key,
                "goal_title": spec.slots.title,
                "goal_success_image": spec.slots.success_image,
                "session_capacity_min": first_plan_adapter.session_min_for(_outcome(slots)),
                "plan": _plan_payload(plan),
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="첫 계획 골든셋 생성 (현재 84건) (쓰기 전용, DB 무관)"
    )
    parser.add_argument("--stdout", action="store_true", help="파일 대신 표준출력으로")
    parser.add_argument(
        "--dump-base-plans",
        action="store_true",
        help="held-out 결함 설계 의뢰용 기준 계획 JSON 만 출력",
    )
    args = parser.parse_args()

    if args.dump_base_plans:
        print(dump_base_plans())
        return

    cases = build_cases()
    payload = to_jsonl(cases)

    if args.stdout:
        print(payload, end="")
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" 고정 — Windows 기본(CRLF)으로 쓰면 재현성 테스트가 OS 마다 갈라진다.
    OUTPUT_PATH.write_text(payload, encoding="utf-8", newline="\n")

    blocks = Counter(c["block"] for c in cases)
    print(f"[build-golden-first-plan-cases] {OUTPUT_PATH.relative_to(_ROOT)}")
    print(f"  총 {len(cases)}건 (기대 {EXPECTED_TOTAL})")
    for block, expected in EXPECTED_COUNTS.items():
        mark = "OK" if blocks[block] == expected else "MISMATCH"
        print(f"  {block:22s} {blocks[block]:3d} / {expected:3d}  {mark}")
    print("  [!] all synthetic=true - report the synthesis ratio explicitly")
    print("  [!] seeded defects are held-out authored - see eval/first_plan_seeded_defects.json")


if __name__ == "__main__":
    main()
