"""주간 재계획이 사용자의 활동 시간대를 지킨다 (planA-5 / calendar-4).

첫 계획은 활동 시간대의 여집합을 수면 busy 로 넣는데(`time_policies_from_outcome`), 재계획은
DB `time_policies` 만 보고 비어 있으면 23:00~08:00 기본값을 썼다. FE 에 time_policies 를
만드는 경로가 없어 실사용자는 늘 기본값이었다 — 저녁에만 된다고 답한 학생도 재계획만 누르면
08시부터 블록이 깔렸다.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from fastapi.testclient import TestClient

from reaction_backend.db.models.user import User
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)
from tests.conftest import FakeActionItemRepo, FakeFixedScheduleRepo, FakeInterviewRepo
from tests.test_replan_route import _finish_session, _freeze_now, _seed_action, _seed_fixed


def _outcome_with_window(start: str, end: str) -> InterviewOutcome:
    return InterviewOutcome(
        session_id="iv_window",
        generated_at=now_kst(),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대3", season="학기중"),
        core_goals=[
            GoalCandidate(
                title="토익",
                category="study",
                is_heaviest=True,
                tentative_tier="focus",
                confidence=0.9,
            )
        ],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start=start, end=end), peak_window=[]
        ),
        preferences=PreferenceProfile(recovery_tone="담백", rest_ok=True, downscope_unit_min=10),
        horizon=None,
    )


def _use_outcome(monkeypatch: Any, repo: FakeInterviewRepo, outcome: InterviewOutcome) -> None:
    import reaction_backend.api.routes.planning as planning_mod

    _finish_session(repo)

    async def _fake_project(row: Any, repo: Any) -> InterviewOutcome:
        return outcome

    monkeypatch.setattr(planning_mod, "_project_session_outcome", _fake_project)


def _starts(resp: Any) -> list[datetime]:
    return [datetime.fromisoformat(b["start"]) for b in resp.json()["blocks"]]


def _seed_backlog(repo: FakeActionItemRepo, n: int = 4) -> None:
    for i in range(n):
        _seed_action(repo, title=f"백로그{i}", est=45, target=date(2026, 7, 16))


def test_replan_places_blocks_inside_an_evening_only_window(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_interview_repo: FakeInterviewRepo,
) -> None:
    """활동 시간대 18~23시 — 기본값(23~08 수면)이었다면 첫 블록이 08:00 에 놓인다."""
    _freeze_now(monkeypatch)
    _use_outcome(monkeypatch, fake_interview_repo, _outcome_with_window("18:00", "23:00"))
    _seed_backlog(fake_action_item_repo)

    resp = client.post("/plans/replan")

    assert resp.status_code == 201, resp.text
    starts = _starts(resp)
    assert starts
    assert all(time(18, 0) <= s.time() < time(23, 0) for s in starts), starts


def test_replan_uses_a_night_owl_window_that_crosses_midnight(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_interview_repo: FakeInterviewRepo,
    fake_fixed_schedule_repo: FakeFixedScheduleRepo,
) -> None:
    """활동 시간대 13:00~02:00 — 자는 시간(02~13시)에는 아무것도 안 놓인다.

    낮(13~22시)을 매일 수업으로 막아 두면 남는 곳은 22:00~02:00 뿐이다. 기본값(23~08 수면)
    이었다면 13시 전 오전(08~13시)에 놓였다.
    """
    _freeze_now(monkeypatch)
    _use_outcome(monkeypatch, fake_interview_repo, _outcome_with_window("13:00", "02:00"))
    _seed_fixed(fake_fixed_schedule_repo, start=time(13, 0), end=time(22, 0))
    _seed_backlog(fake_action_item_repo)

    resp = client.post("/plans/replan")

    assert resp.status_code == 201, resp.text
    starts = _starts(resp)
    assert starts
    assert not any(time(2, 0) <= s.time() < time(13, 0) for s in starts), starts


def test_replan_without_interview_uses_the_window_set_in_settings(
    monkeypatch: Any,
    client: TestClient,
    demo_user_orm: User,
    fake_action_item_repo: FakeActionItemRepo,
    fake_fixed_schedule_repo: FakeFixedScheduleRepo,
) -> None:
    """인터뷰는 없고 설정에서 06:00~21:00 으로 정해 둔 사용자.

    08~21시를 수업으로 막으면 남는 곳은 06~08시다. 기본값(23~08 수면)이었다면 21~23시에
    놓여 사용자가 정한 활동 시간을 넘긴다.
    """
    _freeze_now(monkeypatch)
    demo_user_orm.focus_mode_preferences = {"activity_start": "06:00", "activity_end": "21:00"}
    _seed_fixed(fake_fixed_schedule_repo, start=time(8, 0), end=time(21, 0))
    _seed_backlog(fake_action_item_repo, n=2)

    resp = client.post("/plans/replan")

    assert resp.status_code == 201, resp.text
    starts = _starts(resp)
    assert starts
    assert all(time(6, 0) <= s.time() < time(8, 0) for s in starts), starts
