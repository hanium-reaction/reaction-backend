"""first_plan_adapter.materialize_goals (#96) — 인터뷰 완료/계획 승인 공유 목표 영속.

핵심: 이미 있는 제목의 목표는 재사용(중복 생성 방지), placeholder(#88)는 제외.
인터뷰 완료가 먼저 목표를 저장하고 계획 승인이 같은 목표를 재사용하는 계약을 보증한다.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from reaction_backend.db.models.goal import Goal
from reaction_backend.orchestrator.first_plan_adapter import materialize_goals
from reaction_backend.orchestrator.interview_adapter import PLACEHOLDER_GOAL_TITLE
from reaction_backend.schemas.interview import GoalCandidate


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    """execute → 미리 넣은 기존 목표 반환, add/flush 기록."""

    def __init__(self, existing: list[Goal] | None = None) -> None:
        self._existing = existing or []
        self.added: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:  # noqa: ARG002
        return _Result(self._existing)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _goal(title: str, *, heaviest: bool = False, tier: str = "maintain") -> GoalCandidate:
    return GoalCandidate(
        title=title,
        category="other",
        is_heaviest=heaviest,
        tentative_tier=tier,
        confidence=0.5,
    )


def _placeholder() -> GoalCandidate:
    return GoalCandidate(
        title=PLACEHOLDER_GOAL_TITLE, category="other", tentative_tier="maintain", confidence=0.0
    )


async def test_creates_new_goals_and_picks_heaviest() -> None:
    sess = _FakeSession()
    goals = [_goal("캡스톤", heaviest=True, tier="focus"), _goal("토익")]
    rows, heaviest = await materialize_goals(sess, user_id=uuid4(), core_goals=goals)  # type: ignore[arg-type]

    assert len(rows) == 2
    assert len(sess.added) == 2  # 둘 다 신규 생성
    assert heaviest is not None and heaviest.title == "캡스톤"
    assert heaviest.goal_tier == "focus"


async def test_reuses_existing_goal_by_title() -> None:
    uid = uuid4()
    existing = Goal()
    existing.id = uuid4()
    existing.user_id = uid
    existing.title = "캡스톤"
    existing.goal_tier = "focus"
    existing.archived_at = None
    sess = _FakeSession(existing=[existing])

    goals = [_goal("캡스톤", heaviest=True, tier="focus"), _goal("토익")]
    rows, heaviest = await materialize_goals(sess, user_id=uid, core_goals=goals)  # type: ignore[arg-type]

    # 캡스톤은 재사용(신규 add X) → 토익만 새로 생성
    assert len(sess.added) == 1
    assert sess.added[0].title == "토익"
    assert heaviest is existing  # 기존 행을 heaviest 로
    assert len(rows) == 2


async def test_placeholder_only_yields_no_goals() -> None:
    sess = _FakeSession()
    rows, heaviest = await materialize_goals(
        sess,
        user_id=uuid4(),
        core_goals=[_placeholder()],  # type: ignore[arg-type]
    )
    assert rows == []
    assert heaviest is None
    assert sess.added == []
