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
    """NOW(2026-06-02) 는 화요일 — 굳이 일요일 fixture 를 새로 안 만들려면 아래 non-Sunday 테스트가
    이미 화요일 no-op 을 검증하므로, 여기는 일요일로 맞춰서 실제 집계 경로를 확인한다."""
    user_repo = FakeUserRepo()
    _seed_users(user_repo, [_user(), _user(), _user(anonymized=True)])
    review_repo = FakeReviewRepo()
    sunday = datetime(2026, 6, 7, 20, 0, tzinfo=UTC)  # 2026-06-07 은 일요일

    result = await sweeps.run_weekly_review_sweep(
        sunday, user_repo=user_repo, review_repo=review_repo, session=_FakeSession()
    )
    assert result.total == 2
    assert result.ok == 2
    assert len(review_repo._summaries) == 2


@pytest.mark.asyncio
async def test_weekly_review_sweep_noop_on_non_sunday() -> None:
    """일요일이 아니면 사용자 조회조차 없이 즉시 no-op — 진행 중인 주를 조기 집계해 잠그지 않는다.

    NOW(2026-06-02) 는 화요일. 트리거를 일요일로 좁혀도(`scheduler/runtime.py`) 이 함수가 한
    번 더 막는다 — 수동 호출·설정 드리프트 방어(ADR-0008 §8 "E").
    """
    user_repo = FakeUserRepo()
    _seed_users(user_repo, [_user(), _user()])
    review_repo = FakeReviewRepo()

    result = await sweeps.run_weekly_review_sweep(
        NOW, user_repo=user_repo, review_repo=review_repo, session=_FakeSession()
    )
    assert result == sweeps.SweepResult(total=0, ok=0, failed=0)
    assert len(review_repo._summaries) == 0


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
        "expire_proposed_goals",
        "anonymize_inactive",
        "evening_reflection_notify",
        "pre_card_notify",
        "morning_brief_notify",
        "habit_instances",
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


def test_expire_proposed_goals_job_is_wired_to_the_right_function_and_time() -> None:
    """잠정 목표 만료 cron 이 **매일 04:00 KST 에 만료 job 을** 부른다 (#178).

    회귀: job id 집합만 보는 위 테스트는 (a) 다른 함수를 꽂거나 (b) 시각을 바꿔도 통과한다
    — `expire_reflections` 와 같은 함정. 같은 04:00 배치에 합류시켰는지도 여기서 고정한다.
    """
    from reaction_backend.scheduler import runtime

    job = next(j for j in runtime.build_scheduler().get_jobs() if j.id == "expire_proposed_goals")

    assert job.func is runtime._expire_proposed_goals_job
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "4", f"만료 cron 시각이 04시가 아니다: {fields}"
    assert fields["minute"] == "0", f"만료 cron 분이 00분이 아니다: {fields}"
    assert str(job.trigger.timezone) == "Asia/Seoul"


def test_anonymize_inactive_job_is_wired_to_the_right_function_and_time() -> None:
    """90일 비활성 익명화 cron 이 **매일 04:00 KST 에 익명화 job 을** 부른다 (#24).

    회귀: job id 집합만 보는 위 테스트는 (a) 다른 함수를 꽂거나 (b) 시각을 바꿔도 통과한다.
    이 job 은 특히 중요하다 — DevBaseline §1.4 가 "매일 04:00 KST" 를 잠갔고, 실제로
    **되돌릴 수 없는 마스킹**을 트리거한다. 그리고 이 job 은 오랫동안 "job 함수 미구현 →
    미등록" 상태였다(runtime.py 헤더가 자인). 다시 조용히 빠지면 90일 지난 사용자 데이터가
    영원히 남는데 CI 는 green 이다 — 그 구멍을 여기서 막는다.
    """
    from reaction_backend.scheduler import runtime

    job = next(j for j in runtime.build_scheduler().get_jobs() if j.id == "anonymize_inactive")

    assert job.func is runtime._anonymize_inactive_job
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "4", f"익명화 cron 시각이 04시가 아니다: {fields}"
    assert fields["minute"] == "0", f"익명화 cron 분이 00분이 아니다: {fields}"
    assert str(job.trigger.timezone) == "Asia/Seoul"


def test_weekly_review_job_is_wired_to_the_right_function_and_time() -> None:
    """주간 리뷰가 **일요일 18~23시 30분 폴 KST** 로 precompute sweep 을 부른다 (ADR-0008 §8 "E").

    회귀: job id 집합만 보는 위 테스트는 (a) 다른 함수를 꽂거나 (b) 시각을 바꿔도 통과한다 —
    `expire_reflections` 에서 실증된 회귀 패턴과 동일. 예전 "일요일 03:00 고정 1회"에서
    옮긴 이유(week_window 의 일요일 몫이 거의 안 잡힘)는 `runtime.py` 주석 참고.
    """
    from reaction_backend.scheduler import runtime

    job = next(j for j in runtime.build_scheduler().get_jobs() if j.id == "weekly_review")

    assert job.func is runtime._weekly_review_job
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["day_of_week"] == "sun", f"주간 리뷰 폴이 일요일 한정이 아니다: {fields}"
    assert fields["hour"] == "18-23", f"주간 리뷰 폴 시간대가 18~23시가 아니다: {fields}"
    assert fields["minute"] == "*/30", f"주간 리뷰가 30분 폴이 아니다: {fields}"
    assert str(job.trigger.timezone) == "Asia/Seoul"
    assert job.misfire_grace_time == 600


def test_evening_notify_job_is_wired_to_the_right_function_and_time() -> None:
    """회고 알림이 **19~23시 5분 폴 KST** 로 알림 sweep 을 부른다 (#20).

    5분 폴인 이유: 사용자별 `evening_reflection_time`(19~23시, 분 단위)을 존중하려면
    고정 시각 1회로는 안 된다. 시각·함수를 여기 고정하지 않으면 id 만 남기고
    바꿔치기해도 전 스위트가 통과한다 (expire_reflections 에서 실증된 회귀 패턴).
    """
    from reaction_backend.scheduler import runtime

    job = next(
        j for j in runtime.build_scheduler().get_jobs() if j.id == "evening_reflection_notify"
    )

    assert job.func is runtime._evening_reflection_notify_job
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "19-23", f"회고 알림 폴 시간대가 19~23시가 아니다: {fields}"
    assert fields["minute"] == "*/5", f"회고 알림이 5분 폴이 아니다: {fields}"
    assert str(job.trigger.timezone) == "Asia/Seoul"
    # APScheduler 기본 grace 1초 — 발화 시점 루프가 1초만 밀려도 폴이 통째로 skip 된다.
    assert job.misfire_grace_time == 60


def test_pre_card_notify_job_is_wired_to_the_right_function_and_time() -> None:
    """pre_card 알림이 **종일 5분 폴 KST** 로 알림 sweep 을 부른다 (#20).

    리드타임 2~7분(= 2분 리드 + 5분 폴)은 폴 간격이 5분일 때만 성립 — 간격이 늘어나면
    "카드 2분 전"(architecture.md §6) 약속이 조용히 깨진다.
    """
    from reaction_backend.scheduler import runtime

    job = next(j for j in runtime.build_scheduler().get_jobs() if j.id == "pre_card_notify")

    assert job.func is runtime._pre_card_notify_job
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "*", f"pre_card 폴이 종일이 아니다: {fields}"
    assert fields["minute"] == "*/5", f"pre_card 가 5분 폴이 아니다: {fields}"
    assert str(job.trigger.timezone) == "Asia/Seoul"
    # pre_card 는 창이 이동해 skip 폴을 다음 폴이 회수하지 못한다 — grace 가 필수.
    assert job.misfire_grace_time == 60


def test_morning_brief_notify_job_is_wired_to_the_right_function_and_time() -> None:
    """T2 재관여 알림이 **06~10시 5분 폴 KST** 로 알림 sweep 을 부른다 (근거 대장 §6.2).

    5분 폴인 이유는 evening_reflection_notify 와 같다 — 사용자별 `morning_brief_time`
    (06~10시, 분 단위)을 존중하려면 고정 시각 1회로는 안 된다.
    """
    from reaction_backend.scheduler import runtime

    job = next(j for j in runtime.build_scheduler().get_jobs() if j.id == "morning_brief_notify")

    assert job.func is runtime._morning_brief_notify_job
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "6-10", f"morning_brief 알림 폴 시간대가 06~10시가 아니다: {fields}"
    assert fields["minute"] == "*/5", f"morning_brief 알림이 5분 폴이 아니다: {fields}"
    assert str(job.trigger.timezone) == "Asia/Seoul"
    assert job.misfire_grace_time == 60
