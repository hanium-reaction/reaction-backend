"""Goals 도메인 스키마 (api-contract §6) — S26."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel

GoalTier = Literal["focus", "maintain", "parked"]


class Goal(CamelModel):
    """목표 — GET 응답 항목, POST/PATCH/park 응답."""

    goal_id: str
    title: str
    category: str
    goal_tier: str  # focus | maintain | parked
    priority_level: int
    deadline: str | None  # YYYY-MM-DD
    estimated_minutes: int | None
    status: str  # active | archived | completed


class GoalsByTier(CamelModel):
    """GET /goals 응답 — tier 별 그룹."""

    focus: list[Goal]
    maintain: list[Goal]
    parked: list[Goal]


class GoalCreateRequest(CamelModel):
    """POST /goals 요청."""

    title: str = Field(min_length=1)
    category: str
    goal_tier: GoalTier
    priority_level: int = Field(ge=1, le=5)
    deadline: str | None = None
    estimated_minutes: int | None = None


class GoalUpdateRequest(CamelModel):
    """PATCH /goals/{id} 요청 — 제목·마감·우선순위·tier 변경."""

    title: str | None = None
    deadline: str | None = None
    priority_level: int | None = Field(default=None, ge=1, le=5)
    goal_tier: GoalTier | None = None


class GoalNode(CamelModel):
    """decompose 응답의 노드 (api-contract §6)."""

    node_id: str
    parent_id: str | None
    title: str
    depth: int


class GoalDecomposition(CamelModel):
    """POST /goals/{id}/decompose 응답 — Goal Structuring 결과."""

    goal_id: str
    root_node_id: str
    nodes: list[GoalNode]
