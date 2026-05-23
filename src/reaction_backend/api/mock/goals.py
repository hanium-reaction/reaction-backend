"""Goals mock fixture — #3-D 스텁용 (S26).

데모 목표 + decompose(만다라트 분해) 결과. tier focus/maintain/parked.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DemoGoal:
    """목표 한 건 (api-contract §6)."""

    goal_id: str
    title: str
    category: str
    goal_tier: str  # focus | maintain | parked
    priority_level: int
    deadline: str | None  # YYYY-MM-DD
    estimated_minutes: int | None
    status: str  # active | archived | completed


DEMO_GOALS: tuple[DemoGoal, ...] = (
    DemoGoal(
        "goal_capstone", "캡스톤 프로젝트", "프로젝트", "focus", 1, "2026-07-12", 6000, "active"
    ),
    DemoGoal("goal_toeic", "토익 800점", "시험", "focus", 2, "2026-06-30", 1800, "active"),
    DemoGoal(
        "goal_workout_routine", "주 3회 운동 루틴", "건강", "maintain", 3, None, None, "active"
    ),
)


@dataclass(frozen=True, slots=True)
class DemoGoalNode:
    """decompose 응답의 노드 (api-contract §6)."""

    node_id: str
    parent_node_id: str | None
    title: str
    depth: int
    is_leaf: bool


# 데모 decompose 결과 — goal_capstone 의 만다라트 분해.
DEMO_DECOMPOSITION: tuple[DemoGoalNode, ...] = (
    DemoGoalNode("node_capstone_root", None, "캡스톤", 0, False),
    DemoGoalNode("node_capstone_design", "node_capstone_root", "설계 단계", 1, False),
    DemoGoalNode("node_capstone_impl", "node_capstone_root", "구현 단계", 1, False),
    DemoGoalNode("node_capstone_present", "node_capstone_root", "발표 준비", 1, True),
)
