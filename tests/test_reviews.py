"""Weekly Review — #21-A 슬라이스 (api-contract §13).

3층 검증: ① compute_weekly_kpis 순수 함수 ② GET/POST 라우트 ③ precompute cron job.
LLM 미사용(룰 기반)이라 외부 의존 없음.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.habit import Habit
from reaction_backend.orchestrator.weekly_review import (
    ExecutionStat,
    RecoveryStat,
    compute_effort_minutes,
    compute_weekly_kpis,
)
from reaction_backend.repositories.review_repo import TopFailureContext
from reaction_backend.scheduler.weekly_review_precompute import (
    is_final_summary,
    latest_final_week_start,
    run_weekly_review_for_user,
    week_final_at,
    week_start_of,
)
from reaction_backend.schemas.common import KST
from tests.conftest import (
    DEMO_USER_UUID,
    FakeGoalRepo,
    FakeHabitInstanceRepo,
    FakeHabitRepo,
    FakeReviewRepo,
)

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
    planned_minutes: int | None = None,
    actual_minutes: int | None = None,
) -> ExecutionStat:
    plan = datetime.combine(WEEK + timedelta(days=day_offset), time(hour, 0), tzinfo=KST)
    return ExecutionStat(
        completion_status=status,
        category=category,
        plan_start_at=plan,
        actual_start_at=plan,
        delay_minutes=delay,
        is_recovered=recovered,
        planned_minutes=planned_minutes,
        actual_minutes=actual_minutes,
    )


def _standalone_habit(title: str, *, target: int) -> Habit:
    h = Habit()
    h.id = uuid4()
    h.user_id = DEMO_USER_UUID
    h.title = title
    h.category = "health"
    h.frequency_per_week = target
    h.target_count = target
    h.goal_node_id = None
    h.archived_at = None
    return h


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


def test_get_weekly_carries_effort_on_both_response_paths(
    client: TestClient, fake_review_repo: FakeReviewRepo
) -> None:
    """분 요약이 **즉석 계산 경로와 precomputed 경로 모두**에 실린다 (ADR-0009 D5).

    응답 조립이 두 벌(`_from_kpi` / `_from_summary`)이라 한쪽만 배선하면 다른 쪽은 조용히
    0 이 나간다. 평소 사용자가 타는 건 cron 이 선계산해 둔 precomputed 쪽이다.
    """
    fake_review_repo.seed_execution(
        _exec("done", "routine", 0, 9, planned_minutes=15, actual_minutes=15)
    )
    fake_review_repo.seed_execution(_exec("failed", "study", 1, 14, planned_minutes=180))

    # ① 즉석 계산 경로 (precomputed 없음)
    body = _get(client, WEEK.isoformat()).json()
    assert body["adherenceRate"] == 0.5  # 건수로는 50%
    assert body["effort"] == {
        "plannedMinutes": 195,
        "completedMinutes": 15,
        "actualMinutes": 15,
        "adherenceRate": 0.0769,  # 분으로는 8%
    }

    # ② precomputed 경로 — 같은 값이 나와야 한다.
    client.post("/reviews/weekly/generate", json={"weekStart": WEEK.isoformat()})
    assert (DEMO_USER_UUID, WEEK) in fake_review_repo._summaries
    precomputed = _get(client, WEEK.isoformat()).json()
    assert precomputed["effort"] == body["effort"]


def test_get_weekly_reports_unstarted_blocks_next_to_an_unchanged_adherence(
    client: TestClient, fake_review_repo: FakeReviewRepo
) -> None:
    """1장 끝내고 9장을 손도 안 댄 주 — 준수율은 100% 그대로, `unstartedBlocks` 가 9.

    준수율 정의(시작한 카드만 셈)는 과거 주와의 비교 때문에 바꾸지 않는다(api-contract).
    대신 FE 가 "이번 주, 잘 했어요" 를 띄우기 전에 볼 수 있게 옆에 싣는다. 확정 저장본
    경로에서도 조회 시점에 파생되므로 같은 값이 나간다.
    """
    fake_review_repo.seed_execution(_exec("done", "study", 0, 9))
    fake_review_repo.seed_unstarted_blocks(9)

    live = _get(client, WEEK.isoformat()).json()
    assert live["adherenceRate"] == 1.0
    assert live["unstartedBlocks"] == 9

    async def _finalize() -> None:
        await run_weekly_review_for_user(
            DEMO_USER_UUID, WEEK, week_final_at(WEEK), repo=fake_review_repo, force=True
        )

    asyncio.run(_finalize())
    stored = _get(client, WEEK.isoformat()).json()
    assert stored["unstartedBlocks"] == 9
    generated = client.post("/reviews/weekly/generate", json={"weekStart": WEEK.isoformat()})
    assert generated.json()["unstartedBlocks"] == 9


def test_get_weekly_unstarted_blocks_defaults_to_zero(client: TestClient) -> None:
    assert _get(client, WEEK.isoformat()).json()["unstartedBlocks"] == 0


def test_get_weekly_lists_standalone_habit_check_ins(
    client: TestClient,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
) -> None:
    """습관만 쓴 주도 기록이 보인다 — 카드 실행이 없어 준수율은 여전히 null.

    회귀: KPI 가 카드 실행만 세서, 러닝을 두 번 체크인한 주에 "집계할 활동이 없어요" 가 떴다.
    """
    habit = _standalone_habit("러닝", target=3)
    fake_habit_repo.seed(habit)
    fake_habit_instance_repo.seed_instance(habit.id, WEEK, done=2, target=3)
    fake_habit_instance_repo.seed_instance(habit.id, WEEK - timedelta(days=7), done=3, target=3)

    body = _get(client, WEEK.isoformat()).json()

    assert body["adherenceRate"] is None
    assert body["habits"] == [
        {"habitId": f"habit_{habit.id}", "title": "러닝", "doneCount": 2, "targetCount": 3}
    ]
    generated = client.post("/reviews/weekly/generate", json={"weekStart": WEEK.isoformat()})
    assert generated.json()["habits"] == body["habits"]


def test_standalone_habit_summaries_skip_habits_already_in_the_mandala_section() -> None:
    """만다라 반복형 칸의 습관은 `mandala.habits` 에 이미 있다 — 두 번 나열하지 않는다."""
    from reaction_backend.api.routes.review import _standalone_habit_summaries
    from reaction_backend.db.models.habit_instance import HabitInstance

    mandala_habit = _standalone_habit("만다라 칸 습관", target=5)
    loose_habit = _standalone_habit("물 마시기", target=7)
    instances = []
    for habit, done in ((mandala_habit, 4), (loose_habit, 6)):
        inst = HabitInstance()
        inst.id = uuid4()
        inst.habit_id = habit.id
        inst.habit = habit
        inst.week_start = WEEK
        inst.done_count = done
        inst.target_count = habit.target_count
        instances.append(inst)

    rows = _standalone_habit_summaries(instances, mandala_habit_ids={mandala_habit.id})

    assert [(r.title, r.done_count, r.target_count) for r in rows] == [("물 마시기", 6, 7)]


def test_get_weekly_habits_empty_without_check_ins(client: TestClient) -> None:
    assert _get(client, WEEK.isoformat()).json()["habits"] == []


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


# ──────────── 확정 전 저장본은 믿지 않는다 (일요일 18:00 스냅샷 잠김 회귀) ────────────


def test_week_final_at_is_thursday_after_the_reflection_window() -> None:
    """일요일 카드는 월·화까지 회고할 수 있다 — 확정은 다음 주 목요일 00:00 KST."""
    final = week_final_at(WEEK)
    assert final == datetime.combine(WEEK + timedelta(days=10), time.min, tzinfo=KST)
    assert final.weekday() == 3  # 목요일


def test_latest_final_week_start_skips_the_week_still_open_for_reflection() -> None:
    next_monday = WEEK + timedelta(days=7)
    # 다음 주 수요일 23:59 — WEEK 의 창이 아직 열려 있으니 그 전주가 최근 확정 주.
    wed = datetime.combine(next_monday + timedelta(days=2), time(23, 59), tzinfo=KST)
    assert latest_final_week_start(wed) == WEEK - timedelta(days=7)
    # 목요일 04:30 — WEEK 가 처음 확정된다.
    thu = datetime.combine(next_monday + timedelta(days=3), time(4, 30), tzinfo=KST)
    assert latest_final_week_start(thu) == WEEK


def test_get_weekly_recomputes_while_the_stored_snapshot_is_not_final(
    client: TestClient, fake_review_repo: FakeReviewRepo
) -> None:
    """일요일 18:00 에 저장된 행이 있어도, 그 뒤의 체크인이 점수에 들어간다.

    회귀: GET 이 저장된 행이면 무조건 반환해서 18:00 스냅샷이 그 주 내내 잠겼다 — 21:00 회고
    알림을 받고 [못 함] 을 눌러도 준수율은 100% 그대로였고, 같은 응답의 `effort` 만 바뀌었다.
    """
    sunday_18 = datetime.combine(WEEK + timedelta(days=6), time(18, 0), tzinfo=KST)
    fake_review_repo.seed_execution(_exec("done", "study", 0, 9, planned_minutes=30))

    async def _snapshot() -> None:
        await run_weekly_review_for_user(DEMO_USER_UUID, WEEK, sunday_18, repo=fake_review_repo)

    asyncio.run(_snapshot())
    stored = fake_review_repo._summaries[(DEMO_USER_UUID, WEEK)]
    assert float(stored.adherence_rate) == 1.0
    assert not is_final_summary(stored, WEEK)

    # 21:00 이후 회고 — 일요일 카드 하나를 [못 함] 으로 체크인.
    fake_review_repo.seed_execution(_exec("failed", "study", 6, 14, planned_minutes=30))

    body = _get(client, WEEK.isoformat()).json()
    assert body["adherenceRate"] == 0.5
    # 한 화면 안의 두 준수율이 같은 시점을 본다.
    assert body["effort"]["adherenceRate"] == 0.5


def test_get_weekly_trusts_the_final_snapshot(
    client: TestClient, fake_review_repo: FakeReviewRepo
) -> None:
    """회고 창이 닫힌 뒤 집계한 확정본은 그대로 쓴다 — 지난 주 기록이 흔들리지 않는다."""
    fake_review_repo.seed_execution(_exec("done", "study", 0, 9))
    after_final = week_final_at(WEEK) + timedelta(hours=4, minutes=30)

    async def _finalize() -> None:
        await run_weekly_review_for_user(
            DEMO_USER_UUID, WEEK, after_final, repo=fake_review_repo, force=True
        )

    asyncio.run(_finalize())
    assert is_final_summary(fake_review_repo._summaries[(DEMO_USER_UUID, WEEK)], WEEK)
    # 확정 뒤에 생긴 데이터(있어서는 안 되지만)가 있어도 확정본이 이긴다 — 저장값 경로임을 증명.
    fake_review_repo.seed_execution(_exec("failed", "study", 1, 9))

    assert _get(client, WEEK.isoformat()).json()["adherenceRate"] == 1.0


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


# ────── GET /reviews/weekly — 다음 주기 제안 (ADR-0008 §8 "G" + ADR-0007 PR-4 일반형) ──────
#
# `fetch_promoted_active_goals_for_user`/`fetch_goals_with_milestones`/
# `fetch_action_items_for_leaf_nodes` 는 전부 raw session 을 쓰는데 `_FakeSession.execute()`
# 는 어떤 쿼리를 넣어도 항상 빈 결과다(HTTP 경계 한계, `mandala.habits` 와 같은 이유 —
# `test_mandala_tree_route.py` 참고). 그래서 여기선 필드가 항상 빈 배열로 안전하게 응답에
# 실리는지만 확인한다(만다라 스코프·일반형 둘 다 동일 한계). 실제 판정 로직은
# `test_cycle_proposal.py`(순수 함수) + `test_cycle_proposal_real_db.py`(실 DB, 과거 주기
# 격리 + `fetch_goals_with_milestones` SQL)가 이미 표로 검증했다.


def test_get_weekly_next_cycle_proposals_field_present_and_empty(client: TestClient) -> None:
    resp = _get(client, WEEK.isoformat())
    assert resp.status_code == 200
    assert resp.json()["nextCycleProposals"] == []


def test_get_weekly_goal_completion_proposals_field_present_and_empty(client: TestClient) -> None:
    """ "이 목표 끝난 거 맞아요?" 카드(ADR-0007 6b) — 여기서도 위와 같은 한계다.

    분기 자체(마일스톤이 전부 끝나면 다음 주기 대신 완료 확인)는
    `test_goal_completion_real_db.py` 가 실 DB 로 검증한다.
    """
    resp = _get(client, WEEK.isoformat())
    assert resp.status_code == 200
    assert resp.json()["goalCompletionProposals"] == []


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


def test_proposal_lists_actually_reach_the_response_body() -> None:
    """판정 결과가 **응답까지 실려 나가는지** — 조립 단계를 직접 단언한다.

    빈 배열만 확인하면 배선을 끊어도(`goal_completion_proposals=[]` 로 하드코딩) 아무도
    모른다. 실 DB 테스트는 `_cycle_proposals` 를 **직접** 호출해 HTTP 경계를 안 넘고,
    라우트 테스트는 `_FakeSession` 이라 판정이 항상 빈 결과다 — 둘 사이에 낀 이 조립
    단계가 어느 쪽에도 안 걸린다("쓰기만 하고 읽지 않는" 것과 같은 종류의 구멍이다).

    응답 조립은 저장본·즉석 계산 경로가 **같은 함수**(`_to_response`)라 한 번만 단언하면 된다.
    예전엔 조립 함수가 두 벌이라 한쪽 배선을 끊어도 초록이었다(뮤테이션 확인).
    """
    from uuid import uuid4

    from reaction_backend.api.routes.review import _ReadTimeSections, _to_response
    from reaction_backend.orchestrator.weekly_review import WeeklyKpi
    from reaction_backend.schemas.common import now_kst
    from reaction_backend.schemas.reviews import (
        EffortMinutes,
        GoalCompletionProposal,
        NextCycleProposal,
    )

    resp = _to_response(
        WEEK,
        WeeklyKpi(),
        generated_at=now_kst(),
        sections=_ReadTimeSections(
            effort=EffortMinutes(),
            mandala=None,
            next_cycle_proposals=[NextCycleProposal(goal_id=uuid4(), goal_title="진행 중")],
            goal_completion_proposals=[
                GoalCompletionProposal(goal_id=uuid4(), goal_title="끝낸 것")
            ],
            stale_axis_proposals=[],
            top_failure_contexts=[],
        ),
    )

    body = resp.model_dump(by_alias=True, mode="json")
    assert [p["goalTitle"] for p in body["goalCompletionProposals"]] == ["끝낸 것"]
    assert [p["goalTitle"] for p in body["nextCycleProposals"]] == ["진행 중"]


def test_stored_and_live_paths_return_the_same_body(
    client: TestClient, fake_review_repo: FakeReviewRepo
) -> None:
    """같은 데이터면 확정 저장본 경로와 즉석 계산 경로의 응답이 (생성 시각 빼고) 같다.

    저장본 → KPI 변환(`_kpi_from_summary`)에서 필드 하나를 빠뜨리면 여기서 갈라진다 — 평소
    사용자가 지난주를 볼 때 타는 건 저장본 쪽이다.
    """
    for e in (
        _exec("done", "study", 0, 9, planned_minutes=30, actual_minutes=25),
        _exec("failed", "health", 1, 14, recovered=True, planned_minutes=60),
        _exec("partial_done", "study", 2, 20, delay=15, planned_minutes=45),
    ):
        fake_review_repo.seed_execution(e)
    fake_review_repo.seed_recovery(RecoveryStat(recovery_duration_minutes=20))

    live = _get(client, WEEK.isoformat()).json()

    async def _finalize() -> None:
        await run_weekly_review_for_user(
            DEMO_USER_UUID, WEEK, week_final_at(WEEK), repo=fake_review_repo, force=True
        )

    asyncio.run(_finalize())
    stored = _get(client, WEEK.isoformat()).json()

    assert stored["generatedAt"] != live["generatedAt"]  # 정말 저장본 경로를 탔다
    live.pop("generatedAt")
    stored.pop("generatedAt")
    assert stored == live


def test_get_weekly_reads_the_weeks_executions_once(
    client: TestClient, fake_review_repo: FakeReviewRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """리뷰 탭 한 번 열 때 그 주 실행 표본은 한 번만 읽는다 — `effort` 와 KPI 가 같이 쓴다.

    예전엔 `effort` 용으로 한 번, KPI 용으로 한 번 같은 창을 두 번 읽었다(두 표본 사이에
    체크인이 끼면 한 응답 안의 두 준수율이 다른 시점을 보게 된다).
    """
    calls = 0
    original = fake_review_repo.collect_execution_stats

    async def _counting(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fake_review_repo, "collect_execution_stats", _counting)
    fake_review_repo.seed_execution(_exec("done", "study", 0, 9))

    assert _get(client, WEEK.isoformat()).status_code == 200
    assert calls == 1

    calls = 0
    assert client.post("/reviews/weekly/generate", json={"weekStart": WEEK.isoformat()}).is_success
    assert calls == 1


# ─────────── 분 가중 요약 (ADR-0009 D5) ───────────


def test_effort_minutes_exposes_what_the_count_based_rate_hides() -> None:
    """건수 90% 인 주가 실제로는 절반도 못 한 주일 수 있다 — 그걸 분으로 드러낸다.

    15분짜리 잡무 9개를 끝내고 3시간짜리 하나를 못 하면 `adherence_rate` 는 0.9 다.
    실제로 한 건 135분, 못 한 건 180분 — 계획의 43% 다. 두 숫자가 같은 표본을 보면서
    이렇게 갈리는 게 이 지표의 존재 이유다.
    """
    executions = [
        *(
            _exec("done", "routine", i % 5, 9, planned_minutes=15, actual_minutes=15)
            for i in range(9)
        ),
        _exec("failed", "study", 2, 14, planned_minutes=180),
    ]

    kpi = compute_weekly_kpis(executions, [], WEEK)
    effort = compute_effort_minutes(executions)

    assert kpi.adherence_rate == 0.9  # 건수로는 90%
    assert effort.planned_minutes == 315  # 9×15 + 180
    assert effort.completed_minutes == 135
    assert effort.adherence_rate == 0.4286  # 분으로는 43%


def test_effort_minutes_uses_the_same_sample_as_adherence() -> None:
    """진행 중(in_progress)은 양쪽 다 세지 않는다 — 표본이 갈리면 나란히 놓을 수 없다."""
    executions = [
        _exec("done", "study", 0, 9, planned_minutes=60, actual_minutes=55),
        _exec("in_progress", "study", 0, 14, planned_minutes=120, actual_minutes=30),
    ]

    effort = compute_effort_minutes(executions)

    assert effort.planned_minutes == 60
    assert effort.completed_minutes == 60
    assert effort.actual_minutes == 55
    assert effort.adherence_rate == 1.0


def test_actual_minutes_only_counts_completed_executions() -> None:
    """'예상 대비 실제' 는 완주한 것만 센다 — 중단된 실행의 소요는 비교 대상이 아니다."""
    executions = [
        _exec("done", "study", 0, 9, planned_minutes=60, actual_minutes=90),
        _exec("partial_done", "study", 1, 9, planned_minutes=60, actual_minutes=20),
    ]

    effort = compute_effort_minutes(executions)

    assert effort.planned_minutes == 120  # partial 도 계획에는 있었다
    assert effort.completed_minutes == 60
    assert effort.actual_minutes == 90  # partial 의 20분은 안 센다
    # 예상 60분짜리를 90분에 끝냈다 → 1.5배. 예상이 낙관적이었다는 신호.
    assert effort.actual_minutes / effort.completed_minutes == 1.5


def test_effort_minutes_is_empty_without_data() -> None:
    """표본이 없으면 0/None — 0 나눗셈으로 500 이 되지 않는다."""
    effort = compute_effort_minutes([])
    assert (effort.planned_minutes, effort.completed_minutes, effort.actual_minutes) == (0, 0, 0)
    assert effort.adherence_rate is None
    # 길이를 모르는 옛 데이터만 있어도 마찬가지.
    legacy = compute_effort_minutes([_exec("done", "study", 0, 9)])
    assert legacy.planned_minutes == 0
    assert legacy.adherence_rate is None
