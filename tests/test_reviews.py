"""Weekly Review — #21-A 슬라이스 (api-contract §13).

3층 검증: ① compute_weekly_kpis 순수 함수 ② GET/POST 라우트 ③ precompute cron job.
LLM 미사용(룰 기반)이라 외부 의존 없음.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.orchestrator.weekly_review import (
    ExecutionStat,
    RecoveryStat,
    compute_weekly_kpis,
)
from reaction_backend.repositories.review_repo import TopFailureContext
from reaction_backend.scheduler.weekly_review_precompute import (
    run_weekly_review_for_user,
    week_start_of,
)
from reaction_backend.schemas.common import KST
from tests.conftest import DEMO_USER_UUID, FakeGoalRepo, FakeReviewRepo

# 어떤 날을 넣어도 그 주 월요일 — day_offset 0~6 = 월~일.
WEEK = week_start_of(datetime(2026, 6, 17, tzinfo=KST).date())
NOW = datetime(2026, 6, 21, 3, 0, tzinfo=KST)


def _exec(
    status: str,
    category: str,
    day_offset: int,
    hour: int,
    *,
    recovered: bool = False,
    delay: int | None = 0,
) -> ExecutionStat:
    plan = datetime.combine(WEEK + timedelta(days=day_offset), time(hour, 0), tzinfo=KST)
    return ExecutionStat(
        completion_status=status,
        category=category,
        plan_start_at=plan,
        actual_start_at=plan,
        delay_minutes=delay,
        is_recovered=recovered,
    )


# ───────────────────────── 순수 함수 ─────────────────────────


def test_empty_week_returns_nulls() -> None:
    kpi = compute_weekly_kpis([], [], WEEK)
    assert kpi.adherence_rate is None
    assert kpi.consistency_days is None
    assert kpi.peak_point_window is None
    assert kpi.one_liner is not None and "다음 주" in kpi.one_liner


def test_in_progress_only_is_not_terminal() -> None:
    """미종결(in_progress) 만 있으면 표본 없음 취급."""
    kpi = compute_weekly_kpis([_exec("in_progress", "study", 0, 9)], [], WEEK)
    assert kpi.adherence_rate is None


def test_adherence_rate() -> None:
    execs = [
        _exec("done", "study", 0, 9),
        _exec("over_done", "study", 1, 9),
        _exec("failed", "study", 2, 9),
        _exec("partial_done", "study", 3, 9),
    ]
    kpi = compute_weekly_kpis(execs, [], WEEK)
    assert kpi.adherence_rate == 0.5  # 2 성공 / 4 종결


def test_consistency_longest_streak() -> None:
    # 월·화·수 연속 done + 금 done → 최장 연속 3
    execs = [
        _exec("done", "study", 0, 9),
        _exec("done", "study", 1, 9),
        _exec("done", "study", 2, 9),
        _exec("done", "study", 4, 9),
    ]
    assert compute_weekly_kpis(execs, [], WEEK).consistency_days == 3


def test_resilience_rate() -> None:
    execs = [
        _exec("failed", "study", 0, 9, recovered=True),
        _exec("partial_done", "study", 1, 9, recovered=False),
    ]
    assert compute_weekly_kpis(execs, [], WEEK).resilience_rate == 0.5


def test_category_success_rate() -> None:
    execs = [
        _exec("done", "study", 0, 9),
        _exec("failed", "study", 1, 9),
        _exec("done", "health", 2, 9),
    ]
    rate = compute_weekly_kpis(execs, [], WEEK).category_success_rate
    assert rate == {"study": 0.5, "health": 1.0}


def test_peak_and_drain_window() -> None:
    execs = [
        _exec("done", "study", 1, 9),  # 화 오전 성공
        _exec("done", "study", 1, 10),  # 화 오전 성공
        _exec("failed", "study", 2, 14),  # 수 오후 실패
        _exec("failed", "study", 2, 15),  # 수 오후 실패
    ]
    kpi = compute_weekly_kpis(execs, [], WEEK)
    assert kpi.peak_point_window == "tuesday_morning"
    assert kpi.drain_point_window == "wednesday_afternoon"
    assert "화요일 오전" in (kpi.one_liner or "")


def test_average_recovery_minutes() -> None:
    kpi = compute_weekly_kpis(
        [_exec("done", "study", 0, 9)],
        [RecoveryStat(recovery_duration_minutes=10), RecoveryStat(recovery_duration_minutes=20)],
        WEEK,
    )
    assert kpi.average_recovery_minutes == 15.0


# ───────────────────────── GET /reviews/weekly ─────────────────────────


def _get(client: TestClient, week: str | None = None) -> object:
    params = {"weekStart": week} if week is not None else {}
    return client.get("/reviews/weekly", params=params)


def test_get_weekly_empty(client: TestClient) -> None:
    resp = _get(client, WEEK.isoformat())
    assert resp.status_code == 200
    body = resp.json()
    assert body["weekStart"] == WEEK.isoformat()
    assert body["adherenceRate"] is None
    assert body["oneLiner"]


def test_get_weekly_computes_from_executions(
    client: TestClient, fake_review_repo: FakeReviewRepo
) -> None:
    fake_review_repo.seed_execution(_exec("done", "study", 0, 9))
    fake_review_repo.seed_execution(_exec("failed", "study", 1, 9))
    resp = _get(client, WEEK.isoformat())
    assert resp.status_code == 200
    body = resp.json()
    assert body["adherenceRate"] == 0.5
    assert body["categorySuccessRate"] == {"study": 0.5}


def test_get_weekly_invalid_week(client: TestClient) -> None:
    resp = _get(client, "2026-06")
    assert resp.status_code == 422
    assert resp.json()["code"] == "REVIEW_INVALID_WEEK"


def test_get_weekly_requires_auth(unauthed_client: TestClient) -> None:
    assert unauthed_client.get("/reviews/weekly").status_code == 401


# ───────────────────────── POST /reviews/weekly/generate ─────────────────────────


def test_generate_persists_then_get_returns(
    client: TestClient, fake_review_repo: FakeReviewRepo
) -> None:
    fake_review_repo.seed_execution(_exec("done", "study", 0, 9))
    gen = client.post("/reviews/weekly/generate", json={"weekStart": WEEK.isoformat()})
    assert gen.status_code == 200
    assert gen.json()["adherenceRate"] == 1.0
    # 영속화됨 — get_weekly 가 같은 행 반환
    assert (DEMO_USER_UUID, WEEK) in fake_review_repo._summaries
    got = _get(client, WEEK.isoformat())
    assert got.json()["adherenceRate"] == 1.0


# ──────────── GET /reviews/weekly — 만다라 절 (ADR-0008 §8 "E") ────────────


def _ultimate_goal() -> Goal:
    g = Goal()
    g.id = uuid4()
    g.user_id = DEMO_USER_UUID
    g.title = "궁극목표"
    g.category = "other"
    g.goal_tier = "parked"
    g.status = "active"
    g.is_ultimate = True
    g.archived_at = None
    return g


def _mandala_node(
    *,
    goal_id: object,
    parent_id: object = None,
    title: str = "노드",
    node_type: str = "subgoal",
    depth: int = 1,
    order_index: int = 0,
    completed_at: object = None,
    created_at: object = None,
) -> GoalNode:
    n = GoalNode()
    n.id = uuid4()
    n.goal_id = goal_id
    n.parent_node_id = parent_id
    n.title = title
    n.node_type = node_type
    n.depth = depth
    n.order_index = order_index
    n.is_leaf = node_type == "leaf"
    n.tree_kind = "mandala"
    n.source = "llm"
    n.why_text = None
    n.locked = False
    n.completed_at = completed_at
    n.created_at = created_at or datetime.now(KST)
    n.promoted_goal_id = None
    n.archived_at = None
    return n


def _seed_mandala_tree(repo: FakeGoalRepo, goal: Goal, *, leaf0_completed_at: object) -> None:
    """root + 8축 + 축마다 leaf 1개 — 축0 의 leaf 만 이번 주 완료로 찍는다."""
    repo._items[goal.id] = goal
    root = _mandala_node(goal_id=goal.id, title=goal.title, node_type="core", depth=0)
    subgoals = [
        _mandala_node(goal_id=goal.id, parent_id=root.id, title=f"축{i}", depth=1, order_index=i)
        for i in range(8)
    ]
    leaves = [
        _mandala_node(
            goal_id=goal.id,
            parent_id=subgoals[i].id,
            title=f"축{i}셀0",
            node_type="leaf",
            depth=2,
            completed_at=leaf0_completed_at if i == 0 else None,
        )
        for i in range(8)
    ]
    repo._nodes[goal.id] = [root, *subgoals, *leaves]


def test_get_weekly_mandala_none_without_ultimate_goal(client: TestClient) -> None:
    """궁극목표 자체가 없으면 만다라 절은 응답에서 생략(null) — 못 채우는 변수는 언급 안 함."""
    resp = _get(client, WEEK.isoformat())
    assert resp.status_code == 200
    assert resp.json()["mandala"] is None


def test_get_weekly_mandala_none_without_approved_tree(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    """궁극목표는 있지만 아직 만다라를 승인 안 했으면(트리 없음) 역시 null."""
    fake_goal_repo._items[_ultimate_goal().id] = _ultimate_goal()
    resp = _get(client, WEEK.isoformat())
    assert resp.status_code == 200
    assert resp.json()["mandala"] is None


def test_get_weekly_mandala_reports_completion_and_untouched_axes(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    goal = _ultimate_goal()
    completed_this_week = datetime.combine(WEEK, time(10, 0), tzinfo=KST)
    _seed_mandala_tree(fake_goal_repo, goal, leaf0_completed_at=completed_this_week)

    resp = _get(client, WEEK.isoformat())
    assert resp.status_code == 200
    mandala = resp.json()["mandala"]
    assert mandala is not None
    assert mandala["completedThisWeek"] == 1
    assert mandala["completedTotal"] == 1
    assert mandala["totalLeaves"] == 8
    assert mandala["touchedThisWeek"] == 1
    # 축0 은 완료로 손댔으니 빠지고, 나머지 7축은 아무 활동도 없어 손 못 댄 축.
    assert set(mandala["untouchedAxisTitles"]) == {f"축{i}" for i in range(1, 8)}
    assert "축0" not in mandala["untouchedAxisTitles"]


def test_get_weekly_mandala_excludes_completion_outside_queried_week(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    """조회 대상 주(WEEK)가 아닌 지난주 완료는 completedThisWeek 에 안 잡히고 누적에만 잡힌다."""
    goal = _ultimate_goal()
    last_week_completed = datetime.combine(WEEK - timedelta(days=7), time(10, 0), tzinfo=KST)
    _seed_mandala_tree(fake_goal_repo, goal, leaf0_completed_at=last_week_completed)

    resp = _get(client, WEEK.isoformat())
    mandala = resp.json()["mandala"]
    assert mandala["completedThisWeek"] == 0
    assert mandala["completedTotal"] == 1


def test_generate_weekly_review_includes_mandala_summary(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    """POST /reviews/weekly/generate 도 GET 과 같은 만다라 절을 낸다(단일 소스 재사용)."""
    goal = _ultimate_goal()
    completed_this_week = datetime.combine(WEEK, time(10, 0), tzinfo=KST)
    _seed_mandala_tree(fake_goal_repo, goal, leaf0_completed_at=completed_this_week)

    resp = client.post("/reviews/weekly/generate", json={"weekStart": WEEK.isoformat()})
    assert resp.status_code == 200
    assert resp.json()["mandala"]["completedThisWeek"] == 1


# ────── GET /reviews/weekly — 다음 2주 제안 (ADR-0008 §8 "G") ──────
#
# `fetch_promoted_active_goals_for_user`/`fetch_action_items_for_leaf_nodes` 는 raw session
# 을 쓰는데 `_FakeSession.execute()` 는 어떤 쿼리를 넣어도 항상 빈 결과다(HTTP 경계 한계,
# `mandala.habits` 와 같은 이유 — `test_mandala_tree_route.py` 참고). 그래서 여기선 필드가
# 항상 빈 배열로 안전하게 응답에 실리는지만 확인한다. 실제 판정 로직은
# `test_cycle_proposal.py`(순수 함수) + `test_cycle_proposal_real_db.py`(실 DB, 과거 주기
# 격리)가 이미 표로 검증했다.


def test_get_weekly_next_cycle_proposals_field_present_and_empty(client: TestClient) -> None:
    resp = _get(client, WEEK.isoformat())
    assert resp.status_code == 200
    assert resp.json()["nextCycleProposals"] == []


# ────── GET /reviews/weekly — 실패 사유 상위 3개 (BCT 2.3, 근거 A5, #301) ──────


def test_get_weekly_top_failure_contexts_field_present_and_empty(client: TestClient) -> None:
    """실패 태그가 하나도 없으면(seed 없음) 빈 배열 — FE 는 이때 섹션을 렌더하지 않는다."""
    resp = _get(client, WEEK.isoformat())
    assert resp.status_code == 200
    assert resp.json()["topFailureContexts"] == []


def test_get_weekly_top_failure_contexts_from_repo(
    client: TestClient, fake_review_repo: FakeReviewRepo
) -> None:
    """repo 가 반환한 상위 3개가 camelCase 로 그대로 응답에 실린다."""
    fake_review_repo.seed_top_failure_context(
        TopFailureContext(tag_code="AMBIGUITY", label_ko="모호함", count=4, share=0.4)
    )
    fake_review_repo.seed_top_failure_context(
        TopFailureContext(tag_code="FATIGUE", label_ko="피로", count=3, share=0.3)
    )
    resp = _get(client, WEEK.isoformat())
    assert resp.status_code == 200
    assert resp.json()["topFailureContexts"] == [
        {"tagCode": "AMBIGUITY", "labelKo": "모호함", "count": 4, "share": 0.4},
        {"tagCode": "FATIGUE", "labelKo": "피로", "count": 3, "share": 0.3},
    ]


def test_generate_weekly_review_includes_top_failure_contexts(
    client: TestClient, fake_review_repo: FakeReviewRepo
) -> None:
    fake_review_repo.seed_execution(_exec("done", "study", 0, 9))
    fake_review_repo.seed_top_failure_context(
        TopFailureContext(tag_code="OVERRUN", label_ko="시간 초과", count=1, share=1.0)
    )
    resp = client.post("/reviews/weekly/generate", json={"weekStart": WEEK.isoformat()})
    assert resp.status_code == 200
    assert resp.json()["topFailureContexts"] == [
        {"tagCode": "OVERRUN", "labelKo": "시간 초과", "count": 1, "share": 1.0}
    ]


# ────── GET /reviews/weekly — 손 못 댄 축 제안 (ADR-0008 §6, §8 "H") ──────
#
# 이 판정은 `goal_repo.get_ultimate`/`list_nodes`(둘 다 FakeGoalRepo 메서드, seed 반영됨)와
# `completed_at` 직접체크만으로 되므로(습관 데이터가 필요 없는 프로젝트형 칸 한정) `mandala`
# 절 테스트와 달리 HTTP 레벨에서 실제 로직을 검증할 수 있다. `_stale_axis_proposals` 는
# `?weekStart=` 와 무관하게 실제 "지금"(now_kst) 기준으로 최근 3주를 본다 — 그래서 축
# `created_at` 은 WEEK 상수가 아니라 실제 현재 시각 기준으로 잡는다.


def test_get_weekly_stale_axis_proposal_for_old_untouched_axis(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    """3주 내내 완료도 체크인도 없던 오래된 축만 제안 — 막 만든 축은 제외."""
    goal = _ultimate_goal()
    old_created = datetime.now(KST) - timedelta(days=60)
    recent_created = datetime.now(KST)
    root = _mandala_node(
        goal_id=goal.id, title=goal.title, node_type="core", depth=0, created_at=old_created
    )
    stale_axis = _mandala_node(
        goal_id=goal.id, parent_id=root.id, title="방치축", depth=1, created_at=old_created
    )
    fresh_axis = _mandala_node(
        goal_id=goal.id,
        parent_id=root.id,
        title="새축",
        depth=1,
        order_index=1,
        created_at=recent_created,
    )
    stale_leaf = _mandala_node(
        goal_id=goal.id,
        parent_id=stale_axis.id,
        title="방치칸",
        node_type="leaf",
        depth=2,
        created_at=old_created,
    )
    fresh_leaf = _mandala_node(
        goal_id=goal.id,
        parent_id=fresh_axis.id,
        title="새칸",
        node_type="leaf",
        depth=2,
        created_at=recent_created,
    )
    fake_goal_repo._items[goal.id] = goal
    fake_goal_repo._nodes[goal.id] = [root, stale_axis, fresh_axis, stale_leaf, fresh_leaf]

    resp = _get(client, WEEK.isoformat())
    assert resp.status_code == 200
    proposals = resp.json()["staleAxisProposals"]
    assert [p["axisTitle"] for p in proposals] == ["방치축"]
    assert proposals[0]["axisId"] == str(stale_axis.id)


def test_get_weekly_stale_axis_proposals_empty_without_mandala_tree(client: TestClient) -> None:
    resp = _get(client, WEEK.isoformat())
    assert resp.status_code == 200
    assert resp.json()["staleAxisProposals"] == []


# ───────────────────────── precompute cron ─────────────────────────


@pytest.mark.asyncio
async def test_cron_creates_summary() -> None:
    repo = FakeReviewRepo()
    repo.seed_execution(_exec("done", "study", 0, 9))
    repo.seed_execution(_exec("failed", "study", 1, 9))
    summary = await run_weekly_review_for_user(DEMO_USER_UUID, WEEK, NOW, repo=repo)
    assert float(summary.adherence_rate) == 0.5
    assert (DEMO_USER_UUID, WEEK) in repo._summaries


@pytest.mark.asyncio
async def test_cron_idempotent_skip() -> None:
    """force=False 재실행 — 이미 있으면 재집계 없이 그대로(skip)."""
    repo = FakeReviewRepo()
    repo.seed_execution(_exec("done", "study", 0, 9))
    first = await run_weekly_review_for_user(DEMO_USER_UUID, WEEK, NOW, repo=repo)
    # 두 번째 실행 전에 데이터가 늘어도 skip 이라 반영 안 됨
    repo.seed_execution(_exec("failed", "study", 1, 9))
    second = await run_weekly_review_for_user(DEMO_USER_UUID, WEEK, NOW, repo=repo)
    assert first is second
    assert float(second.adherence_rate) == 1.0  # 첫 집계값 유지


@pytest.mark.asyncio
async def test_cron_force_recomputes() -> None:
    repo = FakeReviewRepo()
    repo.seed_execution(_exec("done", "study", 0, 9))
    await run_weekly_review_for_user(DEMO_USER_UUID, WEEK, NOW, repo=repo)
    repo.seed_execution(_exec("failed", "study", 1, 9))
    forced = await run_weekly_review_for_user(DEMO_USER_UUID, WEEK, NOW, repo=repo, force=True)
    assert float(forced.adherence_rate) == 0.5  # 재집계 반영
