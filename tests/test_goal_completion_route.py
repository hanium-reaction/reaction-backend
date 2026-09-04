"""목표 완료 확정 route — `POST /goals/{goalId}/complete` (ADR-0007 6b).

완료가 **실제로 뜻하는 바**까지 함께 고정한다. `goals.status='completed'` 슬롯은 오래
비어 있었고, 값만 바꾸고 아무 데서도 안 읽으면 `node_type='milestone'` 이 그랬던 것처럼
"쓰기만 하고 읽지 않는" 상태가 된다:

- tier 한도(Focus≤3)를 더 안 먹는다 — 성실히 완주할수록 새 목표를 못 만들면 곤란하다.
- 새 계획 후보에서 빠진다 — 안 빼면 "완료" 배지를 단 채 다음 주기 카드가 쏟아진다.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from reaction_backend.db.models.goal import Goal
from reaction_backend.schemas.common import now_kst
from tests.conftest import DEMO_USER_UUID, FakeGoalRepo, _FakeSession


def _goal(*, title: str = "웹 개발", tier: str = "focus", status: str = "active") -> Goal:
    g = Goal()
    g.id = uuid4()
    g.user_id = DEMO_USER_UUID
    g.title = title
    g.category = "study"
    g.goal_tier = tier
    g.status = status
    g.is_ultimate = False
    g.priority_level = 3
    g.archived_at = None
    return g


def _seed(repo: FakeGoalRepo, goal: Goal) -> Goal:
    repo._items[goal.id] = goal
    return goal


def test_completes_a_goal_and_can_undo(client: TestClient, fake_goal_repo: FakeGoalRepo) -> None:
    goal = _seed(fake_goal_repo, _goal())

    res = client.post(f"/goals/goal_{goal.id}/complete", json={"completed": True})
    assert res.status_code == 200
    assert res.json()["status"] == "completed"

    undo = client.post(f"/goals/goal_{goal.id}/complete", json={"completed": False})
    assert undo.status_code == 200
    assert undo.json()["status"] == "active"


def test_completion_does_not_archive_the_goal(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    """완료는 **보관이 아니다** — 끝낸 것과 치운 것은 다른 뜻이라 목록에 남아야 한다."""
    goal = _seed(fake_goal_repo, _goal())

    client.post(f"/goals/goal_{goal.id}/complete", json={"completed": True})

    assert goal.archived_at is None
    listed = client.get("/goals").json()
    titles = [g["title"] for tier in listed.values() if isinstance(tier, list) for g in tier]
    assert "웹 개발" in titles


def test_completion_is_committed_and_idempotent(
    client: TestClient, fake_goal_repo: FakeGoalRepo, fake_sessions: list[_FakeSession]
) -> None:
    """commit 이 계약이다 — 빠지면 완료가 조용히 유실된다(`get_db` 는 호출자 책임)."""
    goal = _seed(fake_goal_repo, _goal())
    path = f"/goals/goal_{goal.id}/complete"

    first = client.post(path, json={"completed": True})
    second = client.post(path, json={"completed": True})

    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json()["status"] == "completed"
    assert fake_sessions[-1].commit_count == 1


def test_completed_goal_frees_a_tier_slot(client: TestClient, fake_goal_repo: FakeGoalRepo) -> None:
    """Focus 3개를 채운 뒤 하나를 완료하면 새 Focus 목표를 만들 수 있다.

    한도는 "지금 동시에 굴리는 것" 을 제한하는 장치다 — 끝낸 목표가 자리를 계속 잡고
    있으면 완주할수록 새 목표를 못 만든다.
    """
    goals = [_seed(fake_goal_repo, _goal(title=f"목표{i}")) for i in range(3)]
    blocked = client.post(
        "/goals",
        json={"title": "네 번째", "category": "study", "goalTier": "focus", "priorityLevel": 3},
    )
    assert blocked.status_code == 422
    assert blocked.json()["code"] == "GOAL_TIER_LIMIT_EXCEEDED"

    client.post(f"/goals/goal_{goals[0].id}/complete", json={"completed": True})

    allowed = client.post(
        "/goals",
        json={"title": "네 번째", "category": "study", "goalTier": "focus", "priorityLevel": 3},
    )
    assert allowed.status_code == 201


def test_missing_goal_is_404(client: TestClient) -> None:
    res = client.post(f"/goals/goal_{uuid4()}/complete", json={"completed": True})
    assert res.status_code == 404
    assert res.json()["code"] == "GOAL_NOT_FOUND"


def test_archived_goal_is_404(client: TestClient, fake_goal_repo: FakeGoalRepo) -> None:
    goal = _seed(fake_goal_repo, _goal())
    goal.archived_at = now_kst()
    goal.status = "archived"

    res = client.post(f"/goals/goal_{goal.id}/complete", json={"completed": True})

    assert res.status_code == 404
    assert goal.status == "archived"  # 손 안 댐


def test_body_is_required(client: TestClient, fake_goal_repo: FakeGoalRepo) -> None:
    """`completed` 를 빼면 422 — 방향이 없는 요청을 임의로 해석하지 않는다."""
    goal = _seed(fake_goal_repo, _goal())

    res = client.post(f"/goals/goal_{goal.id}/complete", json={})

    assert res.status_code == 422
    assert goal.status == "active"


def test_undo_cannot_smuggle_a_goal_past_the_tier_limit(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    """ "완료 → 새 목표 → 완료 해제" 로 Focus≤3 을 넘길 수 없다 (AGENTS §1 잠금 결정).

    완료하면 한도 집계에서 빠지므로, 되돌아오는 쪽에서 다시 재지 않으면 API 세 번으로
    한도가 뚫린다. 실제로 뚫렸던 경로다 — Focus 3개 → 하나 완료 → 새 목표 201 →
    해제 200 → **활성 Focus 4개**.
    """
    goals = [_seed(fake_goal_repo, _goal(title=f"목표{i}")) for i in range(3)]
    client.post(f"/goals/goal_{goals[0].id}/complete", json={"completed": True})
    created = client.post(
        "/goals",
        json={"title": "네 번째", "category": "study", "goalTier": "focus", "priorityLevel": 3},
    )
    assert created.status_code == 201

    undo = client.post(f"/goals/goal_{goals[0].id}/complete", json={"completed": False})

    assert undo.status_code == 422
    assert undo.json()["code"] == "GOAL_TIER_LIMIT_EXCEEDED"
    assert goals[0].status == "completed"  # 되돌아가지 않았다
    active_focus = [
        g
        for g in fake_goal_repo._items.values()
        if g.status == "active" and g.goal_tier == "focus" and g.archived_at is None
    ]
    assert len(active_focus) == 3


def test_only_active_goals_can_be_completed(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    """`proposed` 는 완료할 수 없다 — 해제하면 `active` 로 나와 승인 게이트를 우회한다.

    `proposed` 는 "인터뷰가 뽑았을 뿐 계획을 승인하지 않은" 잠정 목표다(#176). 완료→해제
    왕복으로 `active` 가 되면 ① tier 한도를 먹기 시작하고 ② 잠정 목표 만료 cron 대상에서
    영구히 빠진다. 승격은 `POST /plans/{planId}/approve` 만 할 수 있어야 한다.
    """
    goal = _seed(fake_goal_repo, _goal(status="proposed"))

    res = client.post(f"/goals/goal_{goal.id}/complete", json={"completed": True})

    assert res.status_code == 422
    assert goal.status == "proposed"  # 손 안 댐


def test_completion_triggers_card_cleanup_but_undo_does_not(
    client: TestClient, fake_goal_repo: FakeGoalRepo, monkeypatch: Any
) -> None:
    """정리는 **완료할 때만** 돈다 — 되돌릴 때는 안 돈다.

    라우트가 보장해야 하는 건 "언제 부르는가" 다. 무엇을 정리하는지(예정 카드만, 사용자가
    옮긴 것은 보존)는 `test_goal_completion_cards_real_db.py` 가 실 DB 로 담당한다 —
    `_FakeSession.execute()` 는 어떤 쿼리든 빈 결과라 여기서는 그 판정이 돌지 않는다.

    되돌릴 때 정리가 돌면 오조작 복구가 **복구가 아니게** 된다.
    """
    from reaction_backend.orchestrator import first_plan_adapter

    calls: list[Any] = []
    axes: list[tuple[bool, bool]] = []

    async def spy(
        session: Any,
        *,
        user_id: Any,
        goal_id: Any,
        include_mandala: bool = False,
        include_recovery: bool = False,
    ) -> int:
        calls.append(goal_id)
        axes.append((include_mandala, include_recovery))
        return 0

    monkeypatch.setattr(first_plan_adapter, "supersede_previous_plan", spy)
    goal = _seed(fake_goal_repo, _goal())

    done = client.post(f"/goals/goal_{goal.id}/complete", json={"completed": True})
    assert done.status_code == 200
    assert calls == [goal.id]
    # #367 — 두 축을 켜서 부르는지까지 본다. 기본값으로 부르면 만다라 유래 카드(궁극목표는
    # 전부 이것)와 회복 카드가 안 멈추는데, 그 판정은 실 DB 테스트에서만 드러나 여기서
    # 조용히 통과해 버린다.
    assert axes == [(True, True)]

    undo = client.post(f"/goals/goal_{goal.id}/complete", json={"completed": False})
    assert undo.status_code == 200  # 응답도 본다 — 안 보면 되돌리기가 통째로 깨져도 초록이다
    assert calls == [goal.id]  # 되돌리기는 정리하지 않는다
