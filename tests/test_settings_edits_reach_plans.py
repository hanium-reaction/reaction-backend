"""'내 정보'에서 고친 집중 길이·집중 시간대가 계획에 닿는다 (critic-5, 계획 쪽).

설정 화면은 "여기서 바꾸면 인터뷰를 다시 하지 않아도 반영돼요" 라고 약속하는데, 계획은
인터뷰 outcome 만 읽어서 `behavioral_profiles` 수정(집중 길이·집중 시간대)이 어디에도 안
닿았다. 인터뷰가 끝난 **뒤에** 바뀌었고 인터뷰 답에서 나올 값과 **다를 때만** 덮는다 — 인터뷰
완료가 같은 값으로 쓴 프로필로 덮으면 목표별 세션 길이 같은 더 구체적인 답을 잃는다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from reaction_backend.api.routes.planning import _apply_edited_profile
from reaction_backend.db.models.behavioral_profile import BehavioralProfile
from reaction_backend.schemas.interview import InterviewOutcome
from tests.conftest import DEMO_USER_UUID, FakeActionItemRepo, FakeInterviewRepo, FakeProfileRepo
from tests.test_replan_route import (
    FROZEN_NOW,
    _craft_outcome,
    _finish_session,
    _freeze_now,
    _seed_action,
)

ENDED = FROZEN_NOW  # `_finish_session` 이 심는 인터뷰 종료 시각


def _outcome() -> InterviewOutcome:
    """세션 60분(목표별)·전역 피크 저녁·목표별 선호 오후."""
    return _craft_outcome(session_min=60, preferred_time="오후")


def _profile(
    *, attention: int = 30, cycle: str = "evening", updated: datetime | None = None
) -> BehavioralProfile:
    row = BehavioralProfile()
    row.user_id = DEMO_USER_UUID
    row.attention_span = attention
    row.energy_cycle = cycle
    row.updated_at = updated if updated is not None else ENDED + timedelta(days=1)
    return row


# ── 순수 규칙 ────────────────────────────────────────────────────────────────


def test_an_attention_span_edited_after_the_interview_sets_the_session_length() -> None:
    got = _apply_edited_profile(_outcome(), _profile(attention=25), interview_ended_at=ENDED)

    assert got.preferences.focus_duration_min == 25
    # 목표별 세션 길이(60)가 남으면 설정에서 바꾼 게 안 보인다 — 비워서 전역 값을 쓰게 한다.
    assert [g.session_length_min for g in got.core_goals] == [None]


def test_the_profile_written_by_the_interview_itself_changes_nothing() -> None:
    """인터뷰 완료가 같은 요청에서 쓴 프로필(종료 직후) — 사용자의 편집이 아니다."""
    outcome = _outcome()
    profile = _profile(attention=25, cycle="night", updated=ENDED + timedelta(seconds=5))

    assert _apply_edited_profile(outcome, profile, interview_ended_at=ENDED) == outcome


def test_an_unchanged_value_keeps_the_per_goal_answers() -> None:
    """피크만 고쳤다 — 집중 길이는 인터뷰 값(기본 30)과 같으니 목표별 60분은 그대로."""
    outcome = _outcome()
    got = _apply_edited_profile(
        outcome, _profile(attention=30, cycle="night"), interview_ended_at=ENDED
    )

    assert got.core_goals[0].session_length_min == 60
    assert got.availability.peak_window == ["심야"]
    assert got.core_goals[0].preferred_time == "오후"  # 목표별 선호는 더 구체적인 답이라 유지


def test_an_out_of_range_attention_span_is_ignored() -> None:
    """설정 검증(5~240분) 밖 값은 오염된 값 — 계획을 2분 카드로 누르지 않는다."""
    outcome = _outcome()
    got = _apply_edited_profile(outcome, _profile(attention=2), interview_ended_at=ENDED)
    assert got.core_goals[0].session_length_min == 60


def test_an_inline_outcome_is_left_alone() -> None:
    """인터뷰 종료 시각을 모르면(인라인 outcome) 편집 여부를 가를 수 없다."""
    outcome = _outcome()
    assert (
        _apply_edited_profile(outcome, _profile(attention=25), interview_ended_at=None) == outcome
    )


# ── 배선 ─────────────────────────────────────────────────────────────────────


def _use_interview(monkeypatch: Any, repo: FakeInterviewRepo) -> None:
    import reaction_backend.api.routes.planning as planning_mod

    _finish_session(repo)

    async def _fake_project(row: Any, repo: Any) -> InterviewOutcome:
        return _outcome()

    monkeypatch.setattr(planning_mod, "_project_session_outcome", _fake_project)


def _block_minutes(resp: Any) -> list[float]:
    return [
        (datetime.fromisoformat(b["end"]) - datetime.fromisoformat(b["start"])).total_seconds() / 60
        for b in resp.json()["blocks"]
    ]


def test_replan_uses_the_attention_span_edited_in_settings(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_interview_repo: FakeInterviewRepo,
    fake_profile_repo: FakeProfileRepo,
) -> None:
    _freeze_now(monkeypatch)
    _use_interview(monkeypatch, fake_interview_repo)
    fake_profile_repo._behavioral[DEMO_USER_UUID] = _profile(attention=25)
    _seed_action(fake_action_item_repo, title="논문 읽기", est=60)

    resp = client.post("/plans/replan")

    assert resp.status_code == 201, resp.text
    minutes = _block_minutes(resp)
    assert minutes and max(minutes) <= 25, minutes


def test_replan_without_a_settings_edit_keeps_the_interview_session_length(
    monkeypatch: Any,
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_interview_repo: FakeInterviewRepo,
    fake_profile_repo: FakeProfileRepo,
) -> None:
    _freeze_now(monkeypatch)
    _use_interview(monkeypatch, fake_interview_repo)
    fake_profile_repo._behavioral[DEMO_USER_UUID] = _profile(
        attention=25,
        updated=ENDED,  # 인터뷰 완료가 쓴 그대로
    )
    _seed_action(fake_action_item_repo, title="논문 읽기", est=60)

    resp = client.post("/plans/replan")

    assert resp.status_code == 201, resp.text
    assert _block_minutes(resp) == [60]


def test_milestones_see_the_edited_attention_span(
    monkeypatch: Any,
    client: TestClient,
    fake_interview_repo: FakeInterviewRepo,
    fake_profile_repo: FakeProfileRepo,
) -> None:
    """계획 생성 쪽 입구(빈 본문 → 최근 인터뷰 복구)도 같은 조립을 탄다."""
    from reaction_backend.orchestrator import first_plan_adapter, first_plan_milestones

    _use_interview(monkeypatch, fake_interview_repo)
    fake_profile_repo._behavioral[DEMO_USER_UUID] = _profile(attention=25)
    seen: dict[str, InterviewOutcome] = {}

    async def no_goal(*args: Any, **kwargs: Any) -> None:
        return None

    async def capture(**kwargs: Any) -> tuple[list[Any], bool]:
        seen["outcome"] = kwargs["outcome"]
        return [], True

    monkeypatch.setattr(first_plan_adapter, "heaviest_goal_id", no_goal)
    monkeypatch.setattr(first_plan_milestones, "generate_milestones", capture)

    resp = client.post("/plans/milestones", json={})

    assert resp.status_code == 200, resp.text
    assert seen["outcome"].preferences.focus_duration_min == 25
