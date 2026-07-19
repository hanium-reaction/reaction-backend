"""Cron sweep — 전체 활성 사용자 순회 (#24).

sweep 이 활성 사용자만 골라 per-user job 을 호출하고, 한 사용자 실패가 배치를 멈추지 않는지
검증. job 자체는 기존(test_scheduler) 에서 검증됨 — 여기선 순회/필터/격리만.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from reaction_backend.db.models.user import User
from reaction_backend.scheduler import sweeps
from tests.conftest import (
    FakeActionItemRepo,
    FakeDailyBriefRepo,
    FakeReviewRepo,
    FakeUserRepo,
    _FakeSession,
)

NOW = datetime(2026, 6, 2, 6, 0, tzinfo=UTC)


def _user(*, state: str = "ACTIVE", anonymized: bool = False, tone: str | None = "gentle") -> User:
    u = User()
    u.id = uuid4()
    u.email = f"{u.id}@reaction.local"
    u.name = "tester"
    u.timezone = "Asia/Seoul"
    u.onboarding_state = state
    u.is_anonymized = anonymized
    u.tone_mode = tone
    u.archived_at = None
    return u


def _seed_users(user_repo: FakeUserRepo, users: list[User]) -> None:
    for u in users:
        user_repo.register(u)


@pytest.mark.asyncio
async def test_morning_brief_sweep_active_only() -> None:
    user_repo = FakeUserRepo()
    active1, active2 = _user(), _user()
    _seed_users(
        user_repo,
        [active1, active2, _user(state="WELCOME"), _user(anonymized=True)],
    )
    brief_repo = FakeDailyBriefRepo()

    result = await sweeps.run_morning_brief_sweep(
        NOW,
        user_repo=user_repo,
        action_repo=FakeActionItemRepo(),
        brief_repo=brief_repo,
        session=_FakeSession(),
    )
    assert result.total == 2  # WELCOME·익명화 제외
    assert result.ok == 2
    assert result.failed == 0
    assert len(brief_repo._items) == 2  # 활성 2명 각각 brief


@pytest.mark.asyncio
async def test_weekly_review_sweep_active_only() -> None:
    user_repo = FakeUserRepo()
    _seed_users(user_repo, [_user(), _user(), _user(anonymized=True)])
    review_repo = FakeReviewRepo()

    result = await sweeps.run_weekly_review_sweep(
        NOW, user_repo=user_repo, review_repo=review_repo, session=_FakeSession()
    )
    assert result.total == 2
    assert result.ok == 2
    assert len(review_repo._summaries) == 2


@pytest.mark.asyncio
async def test_sweep_isolates_one_user_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    user_repo = FakeUserRepo()
    good, bad = _user(), _user()
    _seed_users(user_repo, [good, bad])

    async def _flaky(user_id, now_kst_dt, **kwargs):  # noqa: ANN001, ANN003
        if user_id == bad.id:
            raise RuntimeError("boom")
        return None

    monkeypatch.setattr(sweeps, "run_morning_brief_for_user", _flaky)

    result = await sweeps.run_morning_brief_sweep(
        NOW,
        user_repo=user_repo,
        action_repo=FakeActionItemRepo(),
        brief_repo=FakeDailyBriefRepo(),
        session=_FakeSession(),
    )
    assert result.total == 2
    assert result.ok == 1  # 한 명 실패해도 나머지 진행
    assert result.failed == 1


@pytest.mark.asyncio
async def test_empty_active_users() -> None:
    result = await sweeps.run_morning_brief_sweep(
        NOW,
        user_repo=FakeUserRepo(),
        action_repo=FakeActionItemRepo(),
        brief_repo=FakeDailyBriefRepo(),
        session=_FakeSession(),
    )
    assert result == sweeps.SweepResult(total=0, ok=0, failed=0)


def test_build_scheduler_registers_expected_jobs() -> None:
    """런타임이 import·등록까지 정상(기동 X)."""
    from reaction_backend.scheduler.runtime import build_scheduler

    scheduler = build_scheduler()
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert job_ids == {
        "morning_brief",
        "weekly_review",
        "interruption_resolver",
        "expire_drafts",
        "expire_reflections",
    }


def test_expire_reflections_job_is_wired_to_the_right_function_and_time() -> None:
    """만료 cron 이 **매일 04:00 KST 에 만료 job 을** 부른다.

    회귀: 위 테스트는 job **id 집합**만 본다. 그래서 `id="expire_reflections"` 를 유지한 채
    (a) 다른 함수를 꽂거나 (b) 시각을 바꿔도 전 스위트가 통과했다 — 4일째 카드가 영영 안
    지워져도 CI 는 green 이었다는 뜻이다. 잠금 결정(AGENTS.md §1 "3일 그 이후 자동 만료")의
    '언제·무엇을' 은 여기서만 고정된다.
    """
    from reaction_backend.scheduler import runtime

    job = next(j for j in runtime.build_scheduler().get_jobs() if j.id == "expire_reflections")

    assert job.func is runtime._expire_reflections_job
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "4", f"만료 cron 시각이 04시가 아니다: {fields}"
    assert fields["minute"] == "0", f"만료 cron 분이 00분이 아니다: {fields}"
    assert str(job.trigger.timezone) == "Asia/Seoul"
