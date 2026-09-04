"""축 → 다음 2주 계획 (U14) — 만다라트를 실행으로 잇는 마지막 조각.

여기서 고정하는 계약:
1. 승격은 **멱등** — 같은 축을 두 번 열어도 목표가 두 개 생기지 않는다.
2. 시드의 heaviest 가 **그 축**이다 — 인터뷰 당시 고른 목표가 아니라(다시 세우면 그게 옛것이다).
3. 축의 칸이 **계획 뼈대(마일스톤)** 가 된다 — 분해가 만다라트를 무시하고 다시 지어내지 않게.
4. 돌려주는 건 **Draft** 다 — 카드·블록은 기존 approve 를 눌러야 생긴다(§1.4 자동 적용 금지).

지평 2주 상한(ADR-0008 §3)은 `_max_plan_weeks` 가 실 DB 조회로 판정하므로 fake session 으로는
못 태운다 — `tests/test_mandala_next_cycle_real_db.py` 가 실 Postgres 로 고정한다.
"""

from __future__ import annotations

from datetime import time
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from reaction_backend.api.routes.planning import _max_plan_weeks
from reaction_backend.db.models.behavioral_profile import BehavioralProfile
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.llm import aiClient
from reaction_backend.orchestrator import goal_cycle, interview_adapter, mandala_cycle
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.interview import GoalCandidate
from tests.conftest import (
    DEMO_USER_UUID,
    FakeGoalRepo,
    FakeInterviewRepo,
    FakeProfileRepo,
)
from tests.test_planning_route import (
    _outcome,
    _PromotedTitleSession,
    _seed_finished_session,
    _stub,
)

# ─────────────────────────── 시드 ───────────────────────────


def _behavioral(*, start: time | None, end: time | None) -> BehavioralProfile:
    """온보딩·설정에서 저장된 프로필 1행 — 활동 시간대의 지속 저장소."""
    b = BehavioralProfile()
    b.user_id = DEMO_USER_UUID
    b.energy_cycle = "evening"
    b.attention_span = 50
    b.time_chunk_preference = "60"
    b.preferred_start_time = start
    b.preferred_end_time = end
    return b


def _ultimate(repo: FakeGoalRepo) -> Goal:
    g = Goal()
    g.id = uuid4()
    g.user_id = DEMO_USER_UUID
    g.title = "대기업 개발자로 입사"
    g.category = "other"
    g.goal_tier = "parked"
    g.status = "active"
    g.is_ultimate = True
    g.archived_at = None
    repo._items[g.id] = g
    return g


def _node(
    *,
    goal_id: Any,
    parent_id: Any,
    title: str,
    depth: int,
    order_index: int,
    completed_at: Any = None,
    promoted_goal_id: Any = None,
) -> GoalNode:
    n = GoalNode()
    n.id = uuid4()
    n.goal_id = goal_id
    n.parent_node_id = parent_id
    n.title = title
    n.node_type = {0: "core", 1: "subgoal", 2: "leaf"}[depth]
    n.depth = depth
    n.order_index = order_index
    n.is_leaf = depth == 2
    n.tree_kind = "mandala"
    n.source = "llm"
    n.why_text = f"{title} 이유"
    n.locked = False
    n.completed_at = completed_at
    n.promoted_goal_id = promoted_goal_id
    n.archived_at = None
    return n


def _seed_tree(repo: FakeGoalRepo, goal: Goal, *, cells_per_axis: int = 3) -> dict[str, Any]:
    root = _node(goal_id=goal.id, parent_id=None, title=goal.title, depth=0, order_index=0)
    axis = _node(goal_id=goal.id, parent_id=root.id, title="개발 실력", depth=1, order_index=0)
    cells = [
        _node(
            goal_id=goal.id,
            parent_id=axis.id,
            title=f"칸{i}",
            depth=2,
            order_index=i,
            completed_at=now_kst() if i == 0 else None,  # 칸0 은 이미 끝냄
        )
        for i in range(cells_per_axis)
    ]
    repo._nodes[goal.id] = [root, axis, *cells]
    return {"root": root, "axis": axis, "cells": cells}


# ─────────────────── 순수 시드 합성 (mandala_cycle) ───────────────────


def test_seed_outcome_replaces_core_goals_with_the_axis() -> None:
    """heaviest 가 축으로 갈아끼워진다 — 정체성·가용시간·선호는 사용자가 답한 값 그대로."""
    base = _outcome(focus_goals=2)
    promoted = Goal()
    promoted.id = uuid4()
    promoted.title = "개발 실력"
    promoted.category = "other"
    promoted.goal_tier = "focus"

    seeded = goal_cycle.seed_outcome(base=base, goal=promoted)

    assert [g.title for g in seeded.core_goals] == ["개발 실력"]
    assert seeded.core_goals[0].is_heaviest is True
    assert seeded.availability == base.availability, "가용 시간은 인터뷰 답 그대로"
    assert seeded.identity == base.identity and seeded.preferences == base.preferences


def test_seed_outcome_keeps_slots_the_user_already_answered_for_this_goal() -> None:
    """같은 제목의 목표를 인터뷰에서 이미 답했으면 주당 시간·세션 길이를 버리지 않는다."""
    base = _outcome()
    base = base.model_copy(
        update={
            "core_goals": [
                GoalCandidate(
                    title="개발 실력",
                    category="career",
                    weekly_hours=6,
                    session_length_min=90,
                    frequency_per_week=3,
                    confidence=0.9,
                )
            ]
        }
    )
    promoted = Goal()
    promoted.id = uuid4()
    promoted.title = "개발 실력"
    promoted.category = "other"
    promoted.goal_tier = "focus"

    seeded = goal_cycle.seed_outcome(base=base, goal=promoted)
    got = seeded.core_goals[0]

    assert (got.weekly_hours, got.session_length_min, got.frequency_per_week) == (6, 90, 3)
    assert got.category == "career", "이미 답한 카테고리를 'other' 로 덮지 않는다"
    assert got.is_heaviest is True


def test_cells_as_milestones_skips_completed_and_keeps_order() -> None:
    """완료한 칸은 다시 계획하지 않는다. 순서는 칸 순서 그대로."""
    goal_id, axis_id = uuid4(), uuid4()
    cells = [
        _node(goal_id=goal_id, parent_id=axis_id, title="칸2", depth=2, order_index=2),
        _node(
            goal_id=goal_id,
            parent_id=axis_id,
            title="칸0",
            depth=2,
            order_index=0,
            completed_at=now_kst(),
        ),
        _node(goal_id=goal_id, parent_id=axis_id, title="칸1", depth=2, order_index=1),
    ]

    milestones = mandala_cycle.cells_as_milestones(cells)

    assert [m.title for m in milestones] == ["칸1", "칸2"]
    assert all(m.summary == "" for m in milestones), "없는 요약을 지어내지 않는다"


# ─────────────────── route (U14) ───────────────────


def test_next_cycle_promotes_axis_and_returns_a_draft(
    client: TestClient,
    fake_goal_repo: FakeGoalRepo,
    fake_interview_repo: FakeInterviewRepo,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(aiClient, "run", _stub())
    _seed_finished_session(fake_interview_repo)
    goal = _ultimate(fake_goal_repo)
    ids = _seed_tree(fake_goal_repo, goal)

    res = client.post("/plans/mandala/next-cycle", json={"nodeId": f"node_{ids['axis'].id}"})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["isDraft"] is True, "카드는 승인 전까지 안 생긴다(§1.4)"
    assert body["planId"]
    assert body["axis"]["title"] == "개발 실력"
    assert body["axis"]["newlyPromoted"] is True
    assert body["axis"]["goalTier"] == "focus"
    assert body["axis"]["nodeId"] == f"node_{ids['axis'].id}"
    # 축의 칸이 계획 뼈대가 된다 — 완료한 칸0 은 빠진다.
    assert [m["title"] for m in body["milestones"]] == ["칸1", "칸2"]
    assert ids["axis"].promoted_goal_id is not None


def test_next_cycle_reuses_an_already_promoted_goal(
    client: TestClient,
    fake_goal_repo: FakeGoalRepo,
    fake_interview_repo: FakeInterviewRepo,
    monkeypatch: Any,
) -> None:
    """이미 승격된 축은 그 목표를 그대로 쓴다(`promote` 와 같은 멱등 규칙) — 새로 안 만든다.

    승격 왕복(승격 → 다시 열기)까지 태우는 건 실 DB 몫이다 — fake session 은 `session.add`
    를 버려 방금 만든 Goal 이 repo 에 안 남는다. 여기서는 '이미 승격된 축' 상태를 직접
    시드해 그 분기만 본다.
    """
    monkeypatch.setattr(aiClient, "run", _stub())
    _seed_finished_session(fake_interview_repo)
    goal = _ultimate(fake_goal_repo)
    ids = _seed_tree(fake_goal_repo, goal)

    promoted = Goal()
    promoted.id = uuid4()
    promoted.user_id = DEMO_USER_UUID
    promoted.title = "개발 실력"
    promoted.category = "career"
    promoted.goal_tier = "maintain"  # 요청의 goalTier(focus)가 아니라 기존 tier 가 이긴다
    promoted.status = "active"
    promoted.is_ultimate = False
    promoted.archived_at = None
    fake_goal_repo._items[promoted.id] = promoted
    ids["axis"].promoted_goal_id = promoted.id
    goals_before = len(fake_goal_repo._items)

    res = client.post(
        "/plans/mandala/next-cycle",
        json={"nodeId": f"node_{ids['axis'].id}", "goalTier": "focus"},
    )

    assert res.status_code == 200, res.text
    axis = res.json()["axis"]
    assert axis["newlyPromoted"] is False
    assert axis["goalId"] == f"goal_{promoted.id}"
    assert axis["goalTier"] == "maintain", "이미 승격된 축의 tier 를 요청이 덮지 않는다"
    assert len(fake_goal_repo._items) == goals_before, "중복 목표가 쌓이면 안 된다"


def test_next_cycle_rejects_a_cell(
    client: TestClient,
    fake_goal_repo: FakeGoalRepo,
    fake_interview_repo: FakeInterviewRepo,
) -> None:
    """칸(depth=2)은 주기 단위가 아니다 — `promote` 의 depth 가드와 같은 자리."""
    _seed_finished_session(fake_interview_repo)
    goal = _ultimate(fake_goal_repo)
    ids = _seed_tree(fake_goal_repo, goal)

    res = client.post("/plans/mandala/next-cycle", json={"nodeId": f"node_{ids['cells'][1].id}"})

    assert res.status_code == 422, res.text
    assert res.json()["code"] == "COMMON_VALIDATION_ERROR"
    assert res.json()["field"] == "nodeId"


def test_next_cycle_falls_back_to_the_onboarding_profile(
    client: TestClient,
    fake_goal_repo: FakeGoalRepo,
    fake_profile_repo: FakeProfileRepo,
    monkeypatch: Any,
) -> None:
    """계획 인터뷰가 없어도 **온보딩·설정에 저장된 활동 시간대**가 있으면 계획을 연다.

    지어내는 게 아니라 사용자가 직접 넣은 값을 되돌리는 것이다 — 만다라트만 하고 계획
    인터뷰는 안 한 사용자가 축을 못 여는 게 이 경로의 이유다.
    """
    monkeypatch.setattr(aiClient, "run", _stub())
    fake_profile_repo._behavioral[DEMO_USER_UUID] = _behavioral(start=time(7, 0), end=time(22, 0))
    goal = _ultimate(fake_goal_repo)
    ids = _seed_tree(fake_goal_repo, goal)

    res = client.post("/plans/mandala/next-cycle", json={"nodeId": f"node_{ids['axis'].id}"})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["seedSource"] == "profile"
    assert any("활동 시간대" in w for w in body["warnings"]), "무엇으로 배치했는지 알려준다"
    assert body["axis"]["title"] == "개발 실력"


def test_next_cycle_prefers_the_interview_over_the_profile(
    client: TestClient,
    fake_goal_repo: FakeGoalRepo,
    fake_interview_repo: FakeInterviewRepo,
    fake_profile_repo: FakeProfileRepo,
    monkeypatch: Any,
) -> None:
    """인터뷰가 있으면 그게 이긴다 — 프로필은 인터뷰 답의 파생이라 원본이 더 많은 걸 안다."""
    monkeypatch.setattr(aiClient, "run", _stub())
    _seed_finished_session(fake_interview_repo)
    fake_profile_repo._behavioral[DEMO_USER_UUID] = _behavioral(start=time(7, 0), end=time(22, 0))
    goal = _ultimate(fake_goal_repo)
    ids = _seed_tree(fake_goal_repo, goal)

    res = client.post("/plans/mandala/next-cycle", json={"nodeId": f"node_{ids['axis'].id}"})

    assert res.status_code == 200, res.text
    assert res.json()["seedSource"] == "interview"
    assert not any("활동 시간대" in w for w in res.json()["warnings"])


def test_next_cycle_422_when_neither_interview_nor_activity_window_exists(
    client: TestClient, fake_goal_repo: FakeGoalRepo, fake_profile_repo: FakeProfileRepo
) -> None:
    """활동 시간대를 **어디서도** 모르면 지어내지 않고 422 로 안내한다(v2.01 교훈).

    프로필 행은 있지만 활동창이 비어 있는 경우까지 포함한다 — 피크·집중 길이는 기본값으로
    굴러가지만 활동창은 "언제 배치해도 되는가" 라, 모르면 배치가 통째로 추측이 된다.
    """
    fake_profile_repo._behavioral[DEMO_USER_UUID] = _behavioral(start=None, end=None)
    goal = _ultimate(fake_goal_repo)
    ids = _seed_tree(fake_goal_repo, goal)

    res = client.post("/plans/mandala/next-cycle", json={"nodeId": f"node_{ids['axis'].id}"})

    assert res.status_code == 422, res.text
    assert res.json()["code"] == "COMMON_VALIDATION_ERROR"
    assert "활동 시간대" in res.json()["message"]


def test_next_cycle_can_skip_cells_as_milestones(
    client: TestClient,
    fake_goal_repo: FakeGoalRepo,
    fake_interview_repo: FakeInterviewRepo,
    monkeypatch: Any,
) -> None:
    """끄면 분해가 축 제목만 보고 스스로 뼈대를 만든다(하위호환 경로)."""
    monkeypatch.setattr(aiClient, "run", _stub())
    _seed_finished_session(fake_interview_repo)
    goal = _ultimate(fake_goal_repo)
    ids = _seed_tree(fake_goal_repo, goal)

    res = client.post(
        "/plans/mandala/next-cycle",
        json={"nodeId": f"node_{ids['axis'].id}", "useCellsAsMilestones": False},
    )

    assert res.status_code == 200, res.text
    assert res.json()["milestones"] == []


def test_next_cycle_404_for_unknown_node(
    client: TestClient, fake_interview_repo: FakeInterviewRepo
) -> None:
    _seed_finished_session(fake_interview_repo)

    res = client.post("/plans/mandala/next-cycle", json={"nodeId": f"node_{uuid4()}"})

    assert res.status_code == 404, res.text


# ─────────────────── 2주 상한이 실제로 걸리는가 (ADR-0008 §3) ───────────────────


async def test_axis_seeded_outcome_gets_the_two_week_cap() -> None:
    """이 endpoint 가 2주인 이유 — 새 규칙이 아니라 기존 `_max_plan_weeks` 판정이 걸려서다.

    `seed_outcome` 이 heaviest 제목을 **승격된 목표 제목**으로 맞춰 놓기 때문에 통과한다.
    제목을 안 맞추면(예: 축 노드 제목을 그대로 쓰는데 사용자가 승격 후 목표명을 고친 경우)
    조용히 4주로 떨어진다 — 그래서 여기서 못을 박는다.
    """
    promoted = Goal()
    promoted.id = uuid4()
    promoted.title = "이번 학기 개발 실력"  # 승격 후 사용자가 목표명을 고친 상태
    promoted.category = "other"
    promoted.goal_tier = "focus"
    seeded = goal_cycle.seed_outcome(base=_outcome(), goal=promoted)
    session = _PromotedTitleSession([promoted.title])

    weeks = await _max_plan_weeks(session, uuid4(), seeded)  # type: ignore[arg-type]

    assert seeded.core_goals[0].title == promoted.title
    assert weeks == 2, "만다라 축에서 연 주기는 2주여야 한다(ADR-0008 §3)"


# ─────────────────── 프로필 → 슬롯 (온보딩 폴백의 심장) ───────────────────


def test_slots_from_profile_carries_the_saved_activity_window() -> None:
    """저장된 활동 시간대가 그대로 슬롯이 된다 — 기본값(09:00~23:00)으로 떨어지면 안 된다."""
    behavioral = _behavioral(start=time(7, 0), end=time(22, 30))

    slots = mandala_cycle.slots_from_profile(
        behavioral=behavioral, interaction=None, focus_mode_prefs={}
    )

    assert slots["time.activity_window"] == {
        "type": "range",
        "start": "07:00",
        "end": "22:30",
    }
    outcome = interview_adapter.build_outcome(
        session_id="profile_test",
        slot_answers=slots,
        ambiguity_final=0.0,
        end_reason="completed",
        analysis_source="rule",
    )
    assert outcome.availability.activity_window.start == "07:00"
    assert outcome.availability.activity_window.end == "22:30"
    assert outcome.preferences.focus_duration_min == 50, "집중 길이도 프로필에서 온다"


def test_slots_from_profile_omits_activity_window_when_profile_has_none() -> None:
    """반쪽짜리 프로필로 창을 지어내지 않는다 — 빈 슬롯은 `unresolved_slots` 로 드러난다."""
    slots = mandala_cycle.slots_from_profile(
        behavioral=_behavioral(start=time(7, 0), end=None), interaction=None, focus_mode_prefs={}
    )

    assert "time.activity_window" not in slots
    assert mandala_cycle.has_usable_profile(_behavioral(start=time(7, 0), end=None)) is False
    assert mandala_cycle.has_usable_profile(None) is False
    assert mandala_cycle.has_usable_profile(_behavioral(start=time(7, 0), end=time(22, 0))) is True
