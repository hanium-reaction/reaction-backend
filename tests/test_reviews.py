"""Weekly Review — #21-A 슬라이스 (api-contract §13).

3층 검증: ① compute_weekly_kpis 순수 함수 ② GET/POST 라우트 ③ precompute cron job.
LLM 미사용(룰 기반)이라 외부 의존 없음.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from reaction_backend.orchestrator.weekly_review import (
    ExecutionStat,
    RecoveryStat,
    compute_weekly_kpis,
)
from reaction_backend.scheduler.weekly_review_precompute import (
    run_weekly_review_for_user,
    week_start_of,
)
from reaction_backend.schemas.common import KST
from tests.conftest import DEMO_USER_UUID, FakeReviewRepo

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
