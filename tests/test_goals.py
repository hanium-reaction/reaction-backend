"""Goals — 실 구현 (Issue #22, api-contract §6)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from reaction_backend.db.models.goal import Goal as GoalModel
from tests.conftest import DEMO_USER_UUID, FakeGoalRepo


def _new_goal(client: TestClient, *, title: str = "캡스톤", tier: str = "focus") -> dict[str, Any]:
    resp = client.post(
        "/goals",
        json={
            "title": title,
            "category": "project",
            "goalTier": tier,
            "priorityLevel": 1,
        },
    )
    assert resp.status_code == 201, resp.json()
    return resp.json()


def test_list_empty(client: TestClient) -> None:
    resp = client.get("/goals")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"focus": [], "maintain": [], "parked": []}


def test_create_goal(client: TestClient) -> None:
    body = _new_goal(client)
    assert body["title"] == "캡스톤"
    assert body["goalTier"] == "focus"
    assert body["goalId"].startswith("goal_")
    assert body["status"] == "active"
    # PR7 — 일반 CRUD 로 만든 목표는 만다라 배지 필드가 전부 비활성/null 이어야 한다.
    assert body["isUltimate"] is False
    assert body["promotedFromAxis"] is None


def test_list_goals_exposes_is_ultimate_flag(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    """PR7 — S26 이 parked 목표 카드에 만다라 진입점을 달 수 있게 `isUltimate` 를 내려준다."""
    g = GoalModel()
    g.id = uuid4()
    g.user_id = DEMO_USER_UUID
    g.title = "궁극목표"
    g.category = "other"
    g.goal_tier = "parked"
    g.priority_level = 3
    g.status = "active"
    g.is_ultimate = True
    g.archived_at = None
    fake_goal_repo._items[g.id] = g

    resp = client.get("/goals")

    assert resp.status_code == 200, resp.text
    parked = resp.json()["parked"]
    assert len(parked) == 1
    assert parked[0]["isUltimate"] is True


def test_create_rejects_bad_category(client: TestClient) -> None:
    resp = client.post(
        "/goals",
        json={
            "title": "x",
            "category": "bogus_cat",
            "goalTier": "focus",
            "priorityLevel": 1,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"
    assert resp.json()["field"] == "category"


def test_create_rejects_bad_tier(client: TestClient) -> None:
    resp = client.post(
        "/goals",
        json={
            "title": "x",
            "category": "study",
            "goalTier": "bogus",
            "priorityLevel": 1,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_focus_tier_limit_3(client: TestClient) -> None:
    """Focus 4번째 → 422 GOAL_TIER_LIMIT_EXCEEDED."""
    for i in range(3):
        _new_goal(client, title=f"g{i}", tier="focus")
    resp = client.post(
        "/goals",
        json={
            "title": "over",
            "category": "study",
            "goalTier": "focus",
            "priorityLevel": 1,
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "GOAL_TIER_LIMIT_EXCEEDED"
    assert body["field"] == "goalTier"


def test_maintain_tier_limit_5(client: TestClient) -> None:
    for i in range(5):
        _new_goal(client, title=f"m{i}", tier="maintain")
    resp = client.post(
        "/goals",
        json={
            "title": "over",
            "category": "study",
            "goalTier": "maintain",
            "priorityLevel": 3,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "GOAL_TIER_LIMIT_EXCEEDED"


def test_parked_has_no_limit(client: TestClient) -> None:
    """Parked 자유 — 한도 X (DevBaseline §1.4)."""
    for i in range(10):
        _new_goal(client, title=f"p{i}", tier="parked")
    items = client.get("/goals").json()
    assert len(items["parked"]) == 10


def test_list_groups_by_tier(client: TestClient) -> None:
    _new_goal(client, title="f1", tier="focus")
    _new_goal(client, title="m1", tier="maintain")
    _new_goal(client, title="p1", tier="parked")
    body = client.get("/goals").json()
    assert len(body["focus"]) == 1
    assert len(body["maintain"]) == 1
    assert len(body["parked"]) == 1


def test_update_title(client: TestClient) -> None:
    created = _new_goal(client)
    resp = client.patch(f"/goals/{created['goalId']}", json={"title": "수정"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "수정"


def test_update_category(client: TestClient) -> None:
    """목표 테마 변경 — 생성 후엔 못 바꾸던 것을 PATCH 로 허용 (#326, FE #216 차단 해소)."""
    created = _new_goal(client)
    assert created["category"] == "project"
    resp = client.patch(f"/goals/{created['goalId']}", json={"category": "health"})
    assert resp.status_code == 200
    assert resp.json()["category"] == "health"


def test_update_category_rejects_invalid_value(client: TestClient) -> None:
    created = _new_goal(client)
    resp = client.patch(f"/goals/{created['goalId']}", json={"category": "not-a-category"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_update_omitting_category_keeps_existing_value(client: TestClient) -> None:
    """category 를 생략하면(title 만 보내는 기존 클라이언트) 기존 값이 유지된다."""
    created = _new_goal(client)
    resp = client.patch(f"/goals/{created['goalId']}", json={"title": "제목만 변경"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "제목만 변경"
    assert body["category"] == "project"


def test_update_tier_focus_to_parked(client: TestClient) -> None:
    created = _new_goal(client, tier="focus")
    resp = client.patch(f"/goals/{created['goalId']}", json={"goalTier": "parked"})
    assert resp.status_code == 200
    assert resp.json()["goalTier"] == "parked"


def test_update_tier_rejects_over_limit(client: TestClient) -> None:
    """Focus 3 + Maintain 1 → Maintain 을 Focus 로 변경 시도 → 422."""
    for i in range(3):
        _new_goal(client, title=f"f{i}", tier="focus")
    target = _new_goal(client, title="m", tier="maintain")
    resp = client.patch(f"/goals/{target['goalId']}", json={"goalTier": "focus"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "GOAL_TIER_LIMIT_EXCEEDED"


def test_update_goal_not_found(client: TestClient) -> None:
    resp = client.patch(
        "/goals/goal_99999999-9999-4999-8999-999999999999",
        json={"title": "x"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "GOAL_NOT_FOUND"


def test_update_bad_id_format(client: TestClient) -> None:
    resp = client.patch("/goals/nonexistent", json={"title": "x"})
    assert resp.status_code == 404


def test_update_bad_deadline_format(client: TestClient) -> None:
    created = _new_goal(client)
    resp = client.patch(f"/goals/{created['goalId']}", json={"deadline": "not-a-date"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_nodes_empty_before_plan_approved(client: TestClient) -> None:
    """계획을 아직 승인하지 않은 목표는 트리가 **없다** — 빈 배열이지 404 가 아니다.

    회귀: 예전 `POST /decompose` 는 목표와 무관하게 하드코딩된 데모 트리(캡스톤 → 설계/구현/
    발표)를 돌려줬고 FE 가 그걸 화면에 그렸다. 어떤 목표를 분해해도 같은 캡스톤 단계가 나왔다.
    이제는 실제 분해(계획 승인 시 영속된 goal_nodes)만 읽으므로, 없으면 없다고 답한다.
    """
    created = _new_goal(client)
    resp = client.get(f"/goals/{created['goalId']}/nodes")
    assert resp.status_code == 200
    body = resp.json()
    assert body["goalId"] == created["goalId"]
    assert body["nodes"] == []
    assert body["rootNodeId"] is None


def test_nodes_rejects_unknown_goal(client: TestClient) -> None:
    assert client.get(f"/goals/goal_{uuid4()}/nodes").status_code == 404


def test_decompose_stub_route_is_unregistered(client: TestClient) -> None:
    """옛 mock 경로가 라우팅에서 사라졌다.

    404 로만 확인하면 '없는 목표' 와 구분이 안 되므로 OpenAPI 스키마에서 직접 본다.
    이 경로가 되살아나면 목표와 무관한 데모 트리가 다시 화면에 새어 나간다.
    """
    paths = client.get("/openapi.json").json()["paths"]
    assert "/goals/{goal_id}/decompose" not in paths
    assert "/goals/{goal_id}/nodes" in paths


def test_park_focus_to_parked(client: TestClient) -> None:
    created = _new_goal(client, tier="focus")
    resp = client.post(f"/goals/{created['goalId']}/park")
    assert resp.status_code == 200
    assert resp.json()["goalTier"] == "parked"


def test_park_not_found(client: TestClient) -> None:
    resp = client.post("/goals/goal_99999999-9999-4999-8999-999999999999/park")
    assert resp.status_code == 404


def test_delete_goal(client: TestClient) -> None:
    created = _new_goal(client)
    resp = client.delete(f"/goals/{created['goalId']}")
    assert resp.status_code == 204
    body = client.get("/goals").json()
    assert body == {"focus": [], "maintain": [], "parked": []}


def test_proposed_goals_do_not_consume_tier_limit(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    """잠정(proposed) 목표는 Focus 한도를 잡아먹지 않는다.

    한도는 '동시에 몇 개를 하기로 약속했는가' 다. 인터뷰가 뽑았을 뿐 계획을 승인하지 않은
    목표는 아직 약속이 아니다. 세면 인터뷰만 하고 나간 사용자가 목표를 새로 만들 수 없는데
    이유도 안 보인다(잠정 목표는 화면에 '계획 전' 으로만 뜬다).
    """
    # Focus 2개는 진짜(active), 1개는 잠정 — 한도(3) 기준으로는 2개만 센다.
    _new_goal(client, title="진짜1", tier="focus")
    _new_goal(client, title="진짜2", tier="focus")
    proposed = await_create_proposed(fake_goal_repo, tier="focus")

    resp = client.post(
        "/goals",
        json={"title": "세번째 진짜", "category": "study", "goalTier": "focus", "priorityLevel": 1},
    )
    assert resp.status_code == 201, resp.json()  # 잠정을 셌다면 여기서 422 였다

    # 네 번째는 진짜가 3개가 됐으므로 막힌다 — 한도 자체는 그대로 동작.
    over = client.post(
        "/goals",
        json={"title": "네번째", "category": "study", "goalTier": "focus", "priorityLevel": 1},
    )
    assert over.status_code == 422
    assert over.json()["code"] == "GOAL_TIER_LIMIT_EXCEEDED"
    assert proposed.status == "proposed"  # 잠정 목표는 그대로 남아 있다


def await_create_proposed(repo: FakeGoalRepo, *, tier: str) -> Any:
    """fake repo 에 잠정 목표를 직접 주입 (인터뷰 완료가 만든 상태를 재현)."""
    from uuid import uuid4

    from reaction_backend.db.models.goal import Goal

    g = Goal()
    g.id = uuid4()
    g.user_id = DEMO_USER_UUID
    g.title = "인터뷰가 뽑은 목표"
    g.category = "study"
    g.goal_tier = tier
    g.priority_level = 3
    g.deadline = None
    g.estimated_minutes = None
    g.status = "proposed"
    g.is_ultimate = False
    g.archived_at = None
    repo._items[g.id] = g
    return g


# ─────────────────────────────────────────────────────────────────────────────
# hasPlan — "미계획" 배지의 근거 (#440)
#
# ⚠️ `status` 로는 판정할 수 없다. 계획 승인은 인터뷰가 뽑은 목표를 **전부** `active` 로
# 승격하는데(`first_plan_adapter.materialize_goals`) 계획은 heaviest **하나**에만 생긴다 —
# 로컬 실측으로 계획 없는 active 목표가 **24건**이었고, 그래서 배지가 사라졌다.
# ─────────────────────────────────────────────────────────────────────────────


def _seed_goal_for_has_plan(
    fake_goal_repo: FakeGoalRepo, *, title: str, status: str = "active"
) -> GoalModel:
    g = GoalModel()
    g.id = uuid4()
    g.user_id = DEMO_USER_UUID
    g.title = title
    g.category = "study"
    g.goal_tier = "focus"
    g.priority_level = 1
    g.status = status
    g.is_ultimate = False
    g.archived_at = None
    fake_goal_repo._items[g.id] = g
    return g


class _Node:
    def __init__(self, *, tree_kind: str = "plan", archived_at: Any = None) -> None:
        self.tree_kind = tree_kind
        self.archived_at = archived_at


def test_list_goals_marks_a_planned_goal(client: TestClient, fake_goal_repo: FakeGoalRepo) -> None:
    g = _seed_goal_for_has_plan(fake_goal_repo, title="계획 있는 목표")
    fake_goal_repo._nodes[g.id] = [_Node()]

    body = client.get("/goals").json()
    assert [x["hasPlan"] for x in body["focus"]] == [True]


def test_list_goals_marks_an_active_goal_without_a_plan_as_unplanned(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    """⚠️ **이 케이스가 #440 의 전부다.** 승인이 계획 없는 목표까지 active 로 올린다."""
    _seed_goal_for_has_plan(fake_goal_repo, title="계획 없는 active")

    body = client.get("/goals").json()
    assert [x["hasPlan"] for x in body["focus"]] == [False]


def test_list_goals_marks_a_proposed_goal_as_unplanned(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    """`proposed` 는 정의상 계획이 없다 — 한 판정이 두 경우를 모두 덮는다."""
    _seed_goal_for_has_plan(fake_goal_repo, title="인터뷰만 한 목표", status="proposed")

    body = client.get("/goals").json()
    assert [x["hasPlan"] for x in body["focus"]] == [False]


def test_list_goals_ignores_archived_and_mandala_trees(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    """보관된 계획 트리와 만다라 73칸은 "계획" 이 아니다."""
    from datetime import UTC, datetime

    g1 = _seed_goal_for_has_plan(fake_goal_repo, title="보관된 트리만")
    fake_goal_repo._nodes[g1.id] = [_Node(archived_at=datetime.now(UTC))]
    g2 = _seed_goal_for_has_plan(fake_goal_repo, title="만다라 트리만")
    fake_goal_repo._nodes[g2.id] = [_Node(tree_kind="mandala")]

    body = client.get("/goals").json()
    assert {x["title"]: x["hasPlan"] for x in body["focus"]} == {
        "보관된 트리만": False,
        "만다라 트리만": False,
    }


def test_single_goal_responses_do_not_claim_unplanned(client: TestClient) -> None:
    """단건 응답(create)은 계획 트리를 조회하지 않는다 — 기본값이 `True` 여야 한다.

    기본값을 `False` 로 두면 **방금 만든 목표가 미계획으로 잘못 칠해진다**(목록 새로고침
    전까지). 모르는 것을 "없다" 로 단정하지 않는다.
    """
    body = _new_goal(client, title="새 목표")
    assert body["hasPlan"] is True
