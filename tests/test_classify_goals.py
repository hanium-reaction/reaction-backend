"""scripts.classify_goals.classify_goals — 목표 category 백필 선택 로직 (DB 무관).

category='other' 목표를 활성 액션의 실카테고리 다수결(non-other)로 재분류.
이미 분류된 목표·액션이 전부 'other'거나 없는 목표는 제외.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from scripts.classify_goals import ActionCatRow, GoalRow, classify_goals

U = UUID("11111111-1111-4111-8111-111111111111")


def _goal(category: str = "other", *, title: str = "목표", user: UUID = U) -> GoalRow:
    return GoalRow(id=uuid4(), user_id=user, title=title, category=category)


def test_derives_majority_non_other() -> None:
    g = _goal()
    acts = [
        ActionCatRow(g.id, "health"),
        ActionCatRow(g.id, "health"),
        ActionCatRow(g.id, "other"),  # other 는 다수결에서 제외
    ]
    plan = classify_goals([g], acts)
    assert len(plan.changes) == 1
    assert plan.changes[0].goal_id == g.id
    assert plan.changes[0].old == "other"
    assert plan.changes[0].new == "health"


def test_skips_already_classified_goal() -> None:
    g = _goal(category="study")  # 이미 실카테고리 → 덮어쓰지 않음
    acts = [ActionCatRow(g.id, "health"), ActionCatRow(g.id, "health")]
    plan = classify_goals([g], acts)
    assert plan.changes == []


def test_skips_goal_with_all_other_actions() -> None:
    g = _goal()
    acts = [ActionCatRow(g.id, "other"), ActionCatRow(g.id, "other")]
    plan = classify_goals([g], acts)
    assert plan.changes == []  # 파생 근거 없음 → 진짜 '기타' 유지


def test_skips_goal_with_no_actions() -> None:
    plan = classify_goals([_goal()], [])
    assert plan.changes == []


def test_minority_real_category_wins_over_other() -> None:
    """other 를 제외한 뒤 다수결 — other 가 아무리 많아도 실카테고리가 채택된다."""
    g = _goal()
    acts = [
        *[ActionCatRow(g.id, "other")] * 13,
        *[ActionCatRow(g.id, "study")] * 6,
    ]
    plan = classify_goals([g], acts)
    assert plan.changes[0].new == "study"


def test_per_goal_isolation() -> None:
    """액션 category 는 goal_id 로만 묶인다 — 다른 목표에 새지 않는다."""
    g1, g2 = _goal(title="운동"), _goal(title="코테")
    acts = [ActionCatRow(g1.id, "health"), ActionCatRow(g2.id, "study")]
    plan = classify_goals([g1, g2], acts)
    got = {c.title: c.new for c in plan.changes}
    assert got == {"운동": "health", "코테": "study"}
