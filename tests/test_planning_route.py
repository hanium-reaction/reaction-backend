"""Planning route (#32) — POST /plans/generate 실배선 검증.

ADR-0005 §7.3 패턴: aiClient.run 만 stub (Gemini 미호출). 라우터 → first_plan 그래프
(decompose LLM → schedule 룰 → review LLM) → Draft 응답 경로를 HTTP 레벨로 검증한다.

- 정상 흐름: Draft 응답 + 룰 스케줄러가 action_item 을 가용 시간에 배치.
- 실패 흐름: Focus 한도(≤3) 초과 시 LLM 분해 전 422 GOAL_TIER_LIMIT_EXCEEDED.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from reaction_backend.api.routes.planning import _max_plan_weeks
from reaction_backend.config import get_settings
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.interview_session import InterviewSession as InterviewSessionRow
from reaction_backend.db.models.llm_run import LlmRun
from reaction_backend.db.models.plan_draft import PlanDraft
from reaction_backend.db.session import get_db
from reaction_backend.llm import RunResult, aiClient
from reaction_backend.orchestrator import goal_cycle
from reaction_backend.orchestrator.interview_adapter import PLACEHOLDER_GOAL_TITLE
from reaction_backend.schemas.common import KST, now_kst
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)
from reaction_backend.schemas.planning import (
    ActionItemDraft,
    GoalDecomposition,
    GoalNodeDraft,
    PlanReview,
    ScheduledBlockPreview,
)
from tests.conftest import (
    DEMO_USER_UUID,
    FakeGoalRepo,
    FakeInterviewRepo,
    FakePlanDraftRepo,
    _FakeSession,
)

# ─────────────────────────────────────────────────────────────────────────────
# 픽스처 헬퍼
# ─────────────────────────────────────────────────────────────────────────────


def _outcome(*, focus_goals: int = 1, maintain_goals: int = 0) -> InterviewOutcome:
    """테스트용 InterviewOutcome — focus/maintain 목표 수 조절(한도 초과 케이스용)."""
    goals: list[GoalCandidate] = []
    for i in range(focus_goals):
        goals.append(
            GoalCandidate(
                title=f"focus{i}",
                category="study",
                is_heaviest=(len(goals) == 0),
                tentative_tier="focus",
                confidence=0.9,
            )
        )
    for i in range(maintain_goals):
        goals.append(
            GoalCandidate(
                title=f"maintain{i}",
                category="study",
                is_heaviest=(len(goals) == 0),
                tentative_tier="maintain",
                confidence=0.9,
            )
        )
    return InterviewOutcome(
        session_id="iv_test",
        generated_at=now_kst(),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대3", season="학기중"),
        core_goals=goals,
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:00"),
            peak_window=["오전"],
        ),
        preferences=PreferenceProfile(recovery_tone="담백", rest_ok=True, downscope_unit_min=10),
        horizon=None,
    )


def _body(outcome: InterviewOutcome, *, target_date: str = "2026-06-22") -> dict[str, Any]:
    return {"outcome": outcome.model_dump(by_alias=True, mode="json"), "targetDate": target_date}


def _stub(*, action_items: list[ActionItemDraft] | None = None, fell_back: bool = False) -> Any:
    """aiClient.run stub — decompose(GoalDecomposition) + review(PlanReview) 만 반환."""

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        value: Any
        if schema is GoalDecomposition:
            value = GoalDecomposition(
                goal_nodes=[
                    GoalNodeDraft(
                        node_id="n1",
                        parent_id=None,
                        title="목표0",
                        node_type="root",
                        order_index=0,
                        is_leaf=True,
                    )
                ],
                action_items=action_items or [],
                policy_violations=[],
            )
        elif schema is PlanReview:
            value = PlanReview(approved=True, feedback=[])
        else:  # pragma: no cover - 방어
            raise AssertionError(f"unexpected schema {schema}")
        return RunResult(
            value=value,
            fell_back=fell_back,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    return stub_run


class _CapturingSession(_FakeSession):
    """commit/rollback/add 호출을 기록하는 fake session (로깅·트랜잭션 검증용)."""

    def __init__(self, *, lock_acquired: bool = True) -> None:
        super().__init__(lock_acquired=lock_acquired)
        self.added: list[Any] = []
        self.committed = False
        self.rolled_back = False

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def _use_session(client: TestClient, session: _FakeSession) -> None:
    async def _gen() -> AsyncIterator[_FakeSession]:
        yield session

    client.app.dependency_overrides[get_db] = _gen  # type: ignore[attr-defined]


def _force_provider_timeout(monkeypatch: Any) -> None:
    """provider.generate_structured 를 강제 TimeoutError → tool_executor 룰 fallback 경로.

    aiClient.run 자체는 stub 하지 않아 8s timeout→fallback 의 실제 게이트 로직을 통과시킨다.
    retry 1회로 줄여 테스트를 빠르게 유지.
    """
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    get_settings.cache_clear()

    async def _timeout(**kwargs: Any) -> Any:
        raise TimeoutError

    monkeypatch.setattr("reaction_backend.llm.tool_executor.generate_structured", _timeout)


# ─────────────────────────────────────────────────────────────────────────────
# 정상 흐름
# ─────────────────────────────────────────────────────────────────────────────


def test_generate_returns_draft_plan_with_scheduled_blocks(
    client: TestClient, monkeypatch: Any
) -> None:
    """decompose(LLM) → schedule(룰) → review(LLM) → Draft. action_item 이 가용 시간에 배치."""
    action = ActionItemDraft(
        node_id="n1",
        title="캡스톤 30분 작업",
        estimated_minutes=30,
        category="study",
        first_step="저장소 열기",
    )
    monkeypatch.setattr(aiClient, "run", _stub(action_items=[action]))

    res = client.post("/plans/generate", json=_body(_outcome()))
    assert res.status_code == 200
    body = res.json()

    assert body["isDraft"] is True  # AGENTS §1.4 — 승인 전 항상 Draft
    assert body["aiSource"] == "llm"
    UUID(body["planId"])  # 저장된 Draft 의 실제 id (#62)
    assert body["targetDate"] == "2026-06-22"
    assert body["goalNodes"][0]["nodeId"] == "n1"
    # 룰 스케줄러가 action_item 을 free 블록(09:00~23:00)에 1개 배치
    assert len(body["blocks"]) == 1
    block = body["blocks"][0]
    assert block["title"] == "캡스톤 30분 작업"
    assert block["origin"] == "goal"
    assert block["originId"] == "n1"  # node_id 복원
    assert block["start"].endswith("+09:00")  # KST 응답


def test_generate_marks_rule_source_on_fallback(client: TestClient, monkeypatch: Any) -> None:
    """LLM 룰 fallback(fell_back=True) → 응답 aiSource='rule' (ADR-0005 §7.2)."""
    monkeypatch.setattr(aiClient, "run", _stub(fell_back=True))

    res = client.post("/plans/generate", json=_body(_outcome()))
    assert res.status_code == 200
    assert res.json()["aiSource"] == "rule"


def test_generate_from_interview_session(
    client: TestClient, fake_interview_repo: FakeInterviewRepo, monkeypatch: Any
) -> None:
    """outcome 인라인 없이 interviewSessionId 로 종료 세션의 slot 투영(LLM 0회)."""
    monkeypatch.setattr(aiClient, "run", _stub())

    row = InterviewSessionRow()
    row.id = uuid4()
    row.user_id = DEMO_USER_UUID
    row.end_reason = "completed"
    row.total_turns = 5
    row.ambiguity_final = 0.1
    fake_interview_repo._sessions[row.id] = row
    fake_interview_repo._answers[row.id] = {}

    res = client.post("/plans/generate", json={"interviewSessionId": str(row.id)})
    assert res.status_code == 200
    assert res.json()["isDraft"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 실패 흐름
# ─────────────────────────────────────────────────────────────────────────────


def test_generate_focus_cap_exceeded_returns_422(client: TestClient, monkeypatch: Any) -> None:
    """Focus 목표 4개 → LLM 분해 전 422 GOAL_TIER_LIMIT_EXCEEDED (Validation 게이트)."""

    async def boom(**kwargs: Any) -> RunResult[Any]:  # pragma: no cover - 호출되면 실패
        raise AssertionError("LLM 은 tier 게이트 통과 전에 호출되면 안 됩니다.")

    monkeypatch.setattr(aiClient, "run", boom)

    res = client.post("/plans/generate", json=_body(_outcome(focus_goals=4)))
    assert res.status_code == 422
    assert res.json()["code"] == "GOAL_TIER_LIMIT_EXCEEDED"


def test_generate_requires_outcome_or_session(client: TestClient, monkeypatch: Any) -> None:
    """빈 본문 + 완료된 인터뷰도 없으면 422 COMMON_VALIDATION_ERROR."""
    monkeypatch.setattr(aiClient, "run", _stub())
    res = client.post("/plans/generate", json={})
    assert res.status_code == 422
    assert res.json()["code"] == "COMMON_VALIDATION_ERROR"


def _seed_finished_session(
    repo: FakeInterviewRepo,
    *,
    end_reason: str = "completed",
    ended_at: datetime | None = None,
) -> InterviewSessionRow:
    row = InterviewSessionRow()
    row.id = uuid4()
    row.user_id = DEMO_USER_UUID
    row.kind = "plan"
    row.end_reason = end_reason
    row.total_turns = 5
    row.ambiguity_final = 0.1
    row.ended_at = ended_at if ended_at is not None else now_kst()
    row.used_fallback = False
    repo._sessions[row.id] = row
    repo._answers[row.id] = {}
    return row


def test_generate_empty_body_recovers_latest_interview(
    client: TestClient, fake_interview_repo: FakeInterviewRepo, monkeypatch: Any
) -> None:
    """빈 본문이어도 최근 '정상 종료' 인터뷰로 자동 복구 — FE 가 sessionId 를 잃어도 생성 가능."""
    monkeypatch.setattr(aiClient, "run", _stub())
    _seed_finished_session(fake_interview_repo, ended_at=now_kst() - timedelta(hours=2))
    _seed_finished_session(fake_interview_repo)  # 가장 최근 — 이게 선택돼야 함
    # 더 최신이지만 abandoned — 복구 대상 아님
    _seed_finished_session(
        fake_interview_repo, end_reason="abandoned", ended_at=now_kst() + timedelta(minutes=5)
    )

    res = client.post("/plans/generate", json={})
    assert res.status_code == 200, res.text
    assert res.json()["isDraft"] is True


def test_generate_empty_body_ignores_abandoned_only(
    client: TestClient, fake_interview_repo: FakeInterviewRepo, monkeypatch: Any
) -> None:
    """abandoned 세션만 있으면 복구하지 않고 422 (restart-wins 로 밀려난 미완 세션)."""
    monkeypatch.setattr(aiClient, "run", _stub())
    _seed_finished_session(fake_interview_repo, end_reason="abandoned")

    res = client.post("/plans/generate", json={})
    assert res.status_code == 422
    assert res.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_generate_unknown_interview_session_returns_404(
    client: TestClient, monkeypatch: Any
) -> None:
    """존재하지 않는 interviewSessionId → 404 INTERVIEW_SESSION_NOT_FOUND."""
    monkeypatch.setattr(aiClient, "run", _stub())
    res = client.post("/plans/generate", json={"interviewSessionId": str(uuid4())})
    assert res.status_code == 404
    assert res.json()["code"] == "INTERVIEW_SESSION_NOT_FOUND"


def test_generate_maintain_cap_exceeded_returns_422(client: TestClient, monkeypatch: Any) -> None:
    """Maintain 목표 6개 → 422 GOAL_TIER_LIMIT_EXCEEDED (DevBaseline §1.4 Maintain≤5)."""

    async def boom(**kwargs: Any) -> RunResult[Any]:  # pragma: no cover - 호출되면 실패
        raise AssertionError("LLM 은 tier 게이트 통과 전에 호출되면 안 됩니다.")

    monkeypatch.setattr(aiClient, "run", boom)

    res = client.post("/plans/generate", json=_body(_outcome(focus_goals=0, maintain_goals=6)))
    assert res.status_code == 422
    assert res.json()["code"] == "GOAL_TIER_LIMIT_EXCEEDED"


# ─────────────────────────────────────────────────────────────────────────────
# 8초 timeout → 룰 fallback (강제 timeout) + llm_runs 로깅
# ─────────────────────────────────────────────────────────────────────────────


def test_generate_falls_back_to_rule_on_timeout(client: TestClient, monkeypatch: Any) -> None:
    """provider 강제 TimeoutError → tool_executor 룰 fallback → aiSource='rule' (DoD)."""
    _force_provider_timeout(monkeypatch)

    res = client.post("/plans/generate", json=_body(_outcome()))
    assert res.status_code == 200
    assert res.json()["aiSource"] == "rule"


def test_generate_logs_each_llm_call_to_llm_runs(client: TestClient, monkeypatch: Any) -> None:
    """LLM 호출(decompose·review) 각각 llm_runs 1행 기록 — module/fallback_used 포함 (DoD)."""
    _force_provider_timeout(monkeypatch)
    cap = _CapturingSession()
    _use_session(client, cap)

    res = client.post("/plans/generate", json=_body(_outcome()))
    assert res.status_code == 200

    runs = [o for o in cap.added if isinstance(o, LlmRun)]
    assert len(runs) == 2  # decompose + review (ADR-0005 설계: 2-LLM)
    assert all(r.module == "planning" for r in runs)
    assert all(r.fell_back for r in runs)
    assert {r.prompt_id for r in runs} == {"planning/goal_decompose", "planning/plan_quality"}


# ─────────────────────────────────────────────────────────────────────────────
# Draft 영속화 — GET + generate→approve flow (#62)
# ─────────────────────────────────────────────────────────────────────────────


def _seed_draft(
    repo: FakePlanDraftRepo,
    *,
    blocks: list[ScheduledBlockPreview],
    status: str = "draft",
    expires_in_hours: int = 1,
) -> UUID:
    """fake repo 에 Draft 직접 주입 (정책 위반/만료 분기 테스트용)."""
    action = ActionItemDraft(
        node_id="n1", title="작업", estimated_minutes=30, category="study", first_step="시작"
    )
    node = GoalNodeDraft(
        node_id="n1", parent_id=None, title="목표", node_type="root", order_index=0, is_leaf=True
    )
    payload = {
        "outcome": _outcome().model_dump(mode="json"),
        "goal_nodes": [node.model_dump(mode="json")],
        "action_items": [action.model_dump(mode="json")],
        "blocks": [b.model_dump(mode="json") for b in blocks],
        "warnings": [],
        "policy_violations": [],
        "generated_at": now_kst().isoformat(),
    }
    d = PlanDraft()
    d.id = uuid4()
    d.user_id = DEMO_USER_UUID
    d.status = status
    d.target_date = date(2026, 6, 22)
    d.horizon = None
    d.ai_source = "llm"
    d.payload = payload
    d.expires_at = now_kst() + timedelta(hours=expires_in_hours)
    d.approved_at = None
    repo._items[d.id] = d
    return d.id


def _block(hour: int, minute: int = 0, dur: int = 30) -> ScheduledBlockPreview:
    start = datetime(2026, 6, 22, hour, minute, tzinfo=KST)
    return ScheduledBlockPreview(
        start=start,
        end=start + timedelta(minutes=dur),
        title="작업",
        category="study",
        origin="goal",
        origin_id="n1",
    )


def test_get_plan_returns_saved_draft(client: TestClient, monkeypatch: Any) -> None:
    """generate 로 저장 → GET /plans/{id} 가 같은 Draft 미리보기 재구성."""
    action = ActionItemDraft(
        node_id="n1", title="캡스톤 작업", estimated_minutes=30, category="study", first_step="열기"
    )
    monkeypatch.setattr(aiClient, "run", _stub(action_items=[action]))

    plan_id = client.post("/plans/generate", json=_body(_outcome())).json()["planId"]
    res = client.get(f"/plans/{plan_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["planId"] == plan_id
    assert body["isDraft"] is True
    assert body["actionItems"][0]["title"] == "캡스톤 작업"
    assert len(body["blocks"]) == 1


def test_get_plan_unknown_returns_404(client: TestClient) -> None:
    res = client.get(f"/plans/{uuid4()}")
    assert res.status_code == 404
    assert res.json()["code"] == "PLAN_DRAFT_NOT_FOUND"


def _seed_mandala_draft(repo: FakePlanDraftRepo, *, user_id: UUID = DEMO_USER_UUID) -> UUID:
    """만다라 승인 draft(§3.7) — First Plan payload 와 모양이 다르다(outcome/goal_nodes 없음)."""
    d = PlanDraft()
    d.id = uuid4()
    d.user_id = user_id
    d.status = "draft"
    d.target_date = now_kst().date()
    d.horizon = None
    d.ai_source = "rule"
    d.payload = {
        "kind": "mandala",
        "goal_id": str(uuid4()),
        "center": {"title": "궁극목표", "why_text": None},
        "subgoals": [],
        "cells": [],
        "gaps": [],
    }
    d.expires_at = now_kst() + timedelta(hours=72)
    d.approved_at = None
    repo._items[d.id] = d
    return d.id


def test_get_plan_does_not_500_on_mandala_draft(
    client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo
) -> None:
    """GET /plans/{id} 에 만다라 draft id 를 주면 500 이 아니라 404 여야 한다(PR4).

    이전 가드는 denylist(`kind == "replan"` 만 걸음)라 `kind="mandala"` 는 그냥 통과해
    `_draft_to_response` 가 `payload["goal_nodes"]` 를 찾다 `KeyError` → 500 을 냈다.
    allowlist(`kind` 없음 또는 `"first_plan"` 만 통과) 로 바뀐 뒤에는 막힌다.
    """
    draft_id = _seed_mandala_draft(fake_plan_draft_repo)

    res = client.get(f"/plans/{draft_id}")

    assert res.status_code == 404, res.text
    assert res.json()["code"] == "PLAN_DRAFT_NOT_FOUND"


def test_approve_plan_does_not_500_on_mandala_draft(
    client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo
) -> None:
    """POST /plans/{id}/approve 도 같은 allowlist 가드 — 만다라 draft 는 404(PR4)."""
    draft_id = _seed_mandala_draft(fake_plan_draft_repo)

    res = client.post(f"/plans/{draft_id}/approve")

    assert res.status_code == 404, res.text
    assert res.json()["code"] == "PLAN_DRAFT_NOT_FOUND"


# ─────────────────────────────────────────────────────────────────────────────
# approve — SAVING (goal 트리 영속화 + 가드 롤백 + 3회 재시도 + 만료)
# ─────────────────────────────────────────────────────────────────────────────


def test_approve_persists_goal_tree(client: TestClient, monkeypatch: Any) -> None:
    """generate→approve: goals/goal_nodes/action_items/blocks 영속화, is_draft=false."""
    action = ActionItemDraft(
        node_id="n1", title="작업", estimated_minutes=30, category="study", first_step="시작"
    )
    monkeypatch.setattr(aiClient, "run", _stub(action_items=[action]))
    plan_id = client.post("/plans/generate", json=_body(_outcome())).json()["planId"]

    res = client.post(f"/plans/{plan_id}/approve")
    assert res.status_code == 200
    j = res.json()
    assert j["isDraft"] is False
    assert j["planId"] == plan_id
    assert j["activatedGoals"] == 1
    assert j["activatedGoalNodes"] == 1
    assert j["activatedActionItems"] == 1
    assert j["activatedBlocks"] == 1
    assert j["warnings"] == [], "한도를 안 넘긴 정상 승인은 warnings 가 빈 채로 나간다(#371)"


def test_generate_echoes_confirmed_milestones_in_draft(
    client: TestClient, monkeypatch: Any
) -> None:
    """generate 응답이 확정 마일스톤을 그대로 되비춘다 — approve 가 이걸 다시 읽어
    영속한다(ADR-0007 PR-2)."""
    monkeypatch.setattr(aiClient, "run", _stub())
    body = _body(_outcome())
    body["milestones"] = [
        {"title": "기초 문법", "summary": "변수·조건문"},
        {"title": "배포까지", "summary": ""},
    ]

    res = client.post("/plans/generate", json=body)

    assert res.status_code == 200
    milestones = res.json()["milestones"]
    assert [m["title"] for m in milestones] == ["기초 문법", "배포까지"]


def test_approve_persists_confirmed_milestones_as_nodes(
    client: TestClient, monkeypatch: Any
) -> None:
    """generate(마일스톤 포함)→approve — 마일스톤이 activatedGoalNodes 에 함께 영속된다."""
    action = ActionItemDraft(
        node_id="n1", title="작업", estimated_minutes=30, category="study", first_step="시작"
    )
    monkeypatch.setattr(aiClient, "run", _stub(action_items=[action]))
    body = _body(_outcome())
    body["milestones"] = [
        {"title": "기초 문법", "summary": ""},
        {"title": "배포까지", "summary": ""},
    ]
    plan_id = client.post("/plans/generate", json=body).json()["planId"]

    res = client.post(f"/plans/{plan_id}/approve")

    assert res.status_code == 200
    # 이번 4주 트리(1) + 마일스톤(2) = 3
    assert res.json()["activatedGoalNodes"] == 3


def _placeholder_outcome() -> InterviewOutcome:
    """goals.list 미입력 → core_goals 에 placeholder 1개 + unresolved_slots 기록 (#88)."""
    return InterviewOutcome(
        session_id="iv_ph",
        generated_at=now_kst(),
        end_reason="early_user",
        ambiguity_final=0.5,
        analysis_source="rule",
        identity=IdentityContext(role="대3", season="학기중"),
        core_goals=[
            GoalCandidate(
                title=PLACEHOLDER_GOAL_TITLE,
                category="other",
                tentative_tier="maintain",
                confidence=0.0,
            )
        ],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:00"), peak_window=["오전"]
        ),
        preferences=PreferenceProfile(recovery_tone="담백", rest_ok=True, downscope_ok=True),
        unresolved_slots=["goals.list"],
        horizon=None,
    )


def test_approve_skips_placeholder_goal(client: TestClient, monkeypatch: Any) -> None:
    """goals.list 미입력 시 '(미입력 목표)' placeholder 는 실제 Goal 로 영속되지 않는다 (#88).

    placeholder 만 있으면 소속시킬 goal 이 없어 트리/액션도 만들지 않는다 → 목표 관리
    화면에 정체불명 카드가 노출되지 않는다.
    """
    action = ActionItemDraft(
        node_id="n1", title="작업", estimated_minutes=30, category="study", first_step="시작"
    )
    monkeypatch.setattr(aiClient, "run", _stub(action_items=[action]))
    plan_id = client.post("/plans/generate", json=_body(_placeholder_outcome())).json()["planId"]

    res = client.post(f"/plans/{plan_id}/approve")
    assert res.status_code == 200
    j = res.json()
    assert j["isDraft"] is False
    assert j["activatedGoals"] == 0  # placeholder 제외 → 실제 Goal 0개
    assert j["activatedGoalNodes"] == 0
    assert j["activatedActionItems"] == 0


def test_approve_policy_violation_rolls_back(
    client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo
) -> None:
    """수면(23~09) 시간과 겹치는 블록 → 가드가 롤백 + 422 PLAN_POLICY_VIOLATION."""
    cap = _CapturingSession()
    _use_session(client, cap)
    plan_id = _seed_draft(fake_plan_draft_repo, blocks=[_block(2)])  # 02:00 — 수면 침범

    res = client.post(f"/plans/{plan_id}/approve")
    assert res.status_code == 422
    assert res.json()["code"] == "PLAN_POLICY_VIOLATION"
    assert cap.rolled_back is True
    assert cap.committed is False


def test_approve_expired_draft_returns_410(
    client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo
) -> None:
    """72h 만료된 Draft 승인 → 410 PLAN_DRAFT_EXPIRED (ADR-0005 §7.8)."""
    plan_id = _seed_draft(fake_plan_draft_repo, blocks=[_block(10)], expires_in_hours=-1)

    res = client.post(f"/plans/{plan_id}/approve")
    assert res.status_code == 410
    assert res.json()["code"] == "PLAN_DRAFT_EXPIRED"


def test_approve_completes_onboarding_to_active(
    client: TestClient, demo_user_orm: Any, monkeypatch: Any
) -> None:
    """승인 = 온보딩 완료 신호 → onboarding_state 를 ACTIVE 로 마감(FIRST_PLAN 에서)."""
    demo_user_orm.onboarding_state = "ONBOARDING_FIRST_PLAN"
    monkeypatch.setattr(aiClient, "run", _stub())
    plan_id = client.post("/plans/generate", json=_body(_outcome())).json()["planId"]

    res = client.post(f"/plans/{plan_id}/approve")
    assert res.status_code == 200
    assert demo_user_orm.onboarding_state == "ACTIVE"


def test_approve_completes_onboarding_from_welcome(
    client: TestClient, demo_user_orm: Any, monkeypatch: Any
) -> None:
    """상류 전이(WELCOME→…)가 트리거되지 않아 WELCOME 에 머문 사용자도 승인 시 ACTIVE 로 마감.

    실제 FE 흐름에서 onboarding_state 가 WELCOME 에 고정돼 새로고침 시 재-온보딩되고
    계획이 중복 누적되던 문제를 막는다.
    """
    demo_user_orm.onboarding_state = "WELCOME"
    monkeypatch.setattr(aiClient, "run", _stub())
    plan_id = client.post("/plans/generate", json=_body(_outcome())).json()["planId"]

    res = client.post(f"/plans/{plan_id}/approve")
    assert res.status_code == 200
    assert demo_user_orm.onboarding_state == "ACTIVE"


def test_approve_does_not_regress_onboarding_when_active(
    client: TestClient, demo_user_orm: Any, monkeypatch: Any
) -> None:
    """이미 ACTIVE 인 사용자(재계획 등)는 승인해도 onboarding 후퇴 없음 (멱등)."""
    demo_user_orm.onboarding_state = "ACTIVE"
    monkeypatch.setattr(aiClient, "run", _stub())
    plan_id = client.post("/plans/generate", json=_body(_outcome())).json()["planId"]

    res = client.post(f"/plans/{plan_id}/approve")
    assert res.status_code == 200
    assert demo_user_orm.onboarding_state == "ACTIVE"


class _RetryFailSession(_CapturingSession):
    """flush 가 처음 `fail_times` 회 RuntimeError → 3회 재시도(ADR-0005 §2.5.1) 검증용."""

    def __init__(self, *, fail_times: int) -> None:
        super().__init__()
        self._fail_times = fail_times
        self.flush_count = 0

    async def flush(self) -> None:
        self.flush_count += 1
        if self.flush_count <= self._fail_times:
            raise RuntimeError("simulated flush failure")


def test_approve_retries_then_succeeds(
    client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo
) -> None:
    """flush 가 2회 실패 후 성공 → 3회 재시도 내 영속화 성공 (200)."""
    session = _RetryFailSession(fail_times=2)
    _use_session(client, session)
    plan_id = _seed_draft(fake_plan_draft_repo, blocks=[_block(10)])

    res = client.post(f"/plans/{plan_id}/approve")
    assert res.status_code == 200
    assert session.flush_count >= 3  # 최소 2회 실패 + 성공 시도


def test_approve_save_failure_returns_500(
    client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo
) -> None:
    """flush 가 매번 실패 → 3회 재시도 후 500 PLAN_SAVE_FAILED."""
    session = _RetryFailSession(fail_times=99)
    _use_session(client, session)
    plan_id = _seed_draft(fake_plan_draft_repo, blocks=[_block(10)])

    res = client.post(f"/plans/{plan_id}/approve")
    assert res.status_code == 500
    assert res.json()["code"] == "PLAN_SAVE_FAILED"


def test_approve_already_approved_is_idempotent_without_reapply(
    client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo
) -> None:
    """이미 승인된 Draft 재승인 → 스냅샷 카운트만 반환, 재영속화(INSERT) 없음.

    재승인이 매번 INSERT 되면 같은 날짜에 카드/블록이 겹겹이 누적된다(중복 블록 버그).
    """
    cap = _CapturingSession()
    _use_session(client, cap)
    plan_id = _seed_draft(fake_plan_draft_repo, blocks=[_block(10)], status="approved")

    res = client.post(f"/plans/{plan_id}/approve")
    assert res.status_code == 200
    j = res.json()
    assert j["activatedActionItems"] == 1  # 저장 스냅샷 길이 기반
    assert j["activatedBlocks"] == 1
    assert cap.added == []  # 아무것도 다시 영속화하지 않음


def test_approve_requires_lock_before_checks(
    client: TestClient, fake_plan_draft_repo: FakePlanDraftRepo
) -> None:
    """Draft 검사도 lock 안 — lock 미획득이면 어떤 검사·응답도 없이 409.

    검사(status)와 영속화 사이에 다른 요청이 끼면 같은 Draft 가 이중 영속화되던
    race(동시 더블 승인)를 lock 순서로 봉합했는지 확인한다. **approved** Draft 를 쓰는
    이유: 검사가 lock 밖이던 과거 코드는 lock 이전에 200(멱등)을 반환해 버려서,
    draft 상태로는 신·구 코드가 구분되지 않는다 — approved+lock 미획득 → 409 여야
    검사가 lock 뒤로 이동했음이 증명된다.
    """
    _use_session(client, _FakeSession(lock_acquired=False))
    plan_id = _seed_draft(fake_plan_draft_repo, blocks=[_block(10)], status="approved")

    res = client.post(f"/plans/{plan_id}/approve")
    assert res.status_code == 409
    assert res.json()["code"] == "AGENT_CONCURRENT_ACCESS"


def test_milestones_endpoint_returns_list(client: TestClient, monkeypatch: Any) -> None:
    """POST /plans/milestones (Stage A) — LLM 이 준 중간 목표 목록을 그대로 돌려준다."""
    from reaction_backend.schemas.planning import MilestoneDraft, MilestonePlan

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        assert kwargs["schema"] is MilestonePlan  # 마일스톤 스키마로 호출됨
        value = MilestonePlan(
            milestones=[
                MilestoneDraft(title="기초 문법", summary="변수·함수·조건문"),
                MilestoneDraft(title="DOM 조작", summary="이벤트·렌더링"),
                MilestoneDraft(title="토이 프로젝트", summary="배포까지"),
            ]
        )
        return RunResult(
            value=value,
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)
    res = client.post("/plans/milestones", json=_body(_outcome()))
    assert res.status_code == 200
    body = res.json()
    assert [m["title"] for m in body["milestones"]] == ["기초 문법", "DOM 조작", "토이 프로젝트"]
    assert body["aiSource"] == "llm"


def test_milestones_endpoint_returns_saved_skeleton_without_calling_llm(
    client: TestClient, monkeypatch: Any
) -> None:
    """이미 확정·영속된 뼈대가 있으면 **LLM 을 아예 안 부르고** 그걸 돌려준다 (ADR-0007 PR-2.5).

    2주기 이후의 정상 경로다. 매번 새로 지어내면 사용자가 1주기에 확정한 목록과 다른
    목록이 나오고, 승인이 그 목록을 뼈대로 반영하므로(PR-6a) 사용자가 세운 구조가
    주기마다 새 LLM 초안으로 갈린다.

    SQL 술어 자체(미보관·plan 트리·order_index 정렬)는 여기서 검증하지 않는다 — 라우트
    테스트의 fake session 은 WHERE/ORDER BY 를 평가하지 않는다.
    `tests/test_first_plan_milestones_real_db.py` 가 실 Postgres 로 담당하고, 여기서는
    **배선**(저장분이 있으면 LLM 으로 안 내려간다)만 본다.
    """
    from reaction_backend.orchestrator import first_plan_adapter
    from reaction_backend.schemas.planning import MilestoneDraft

    async def never(**kwargs: Any) -> RunResult[Any]:
        raise AssertionError("저장된 뼈대가 있는데 LLM 을 불렀다")

    async def fake_goal_id(*args: Any, **kwargs: Any) -> UUID:
        return uuid4()

    async def fake_saved(*args: Any, **kwargs: Any) -> list[MilestoneDraft]:
        return [
            MilestoneDraft(title="기초 문법", summary="변수·함수·조건문"),
            MilestoneDraft(title="배포까지", summary=""),
        ]

    monkeypatch.setattr(aiClient, "run", never)
    monkeypatch.setattr(first_plan_adapter, "heaviest_goal_id", fake_goal_id)
    monkeypatch.setattr(first_plan_adapter, "fetch_confirmed_milestones", fake_saved)

    res = client.post("/plans/milestones", json=_body(_outcome()))

    assert res.status_code == 200
    body = res.json()
    assert [m["title"] for m in body["milestones"]] == ["기초 문법", "배포까지"]
    assert body["aiSource"] == "saved"


def test_milestones_endpoint_generates_when_goal_has_no_saved_skeleton(
    client: TestClient, monkeypatch: Any
) -> None:
    """목표 행은 있는데(인터뷰 완료가 proposed 로 저장) 아직 승인 전이면 LLM 으로 내려간다.

    1주기의 정상 경로 — 저장분 조회가 빈 목록을 주는 것과 "목표를 못 찾음"을 구분하지
    않고 똑같이 생성으로 떨어지는지 본다.
    """
    from reaction_backend.orchestrator import first_plan_adapter
    from reaction_backend.schemas.planning import MilestoneDraft, MilestonePlan

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        return RunResult(
            value=MilestonePlan(milestones=[MilestoneDraft(title="새 뼈대", summary="")]),
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    async def fake_goal_id(*args: Any, **kwargs: Any) -> UUID:
        return uuid4()

    async def no_saved(*args: Any, **kwargs: Any) -> list[MilestoneDraft]:
        return []

    monkeypatch.setattr(aiClient, "run", stub_run)
    monkeypatch.setattr(first_plan_adapter, "heaviest_goal_id", fake_goal_id)
    monkeypatch.setattr(first_plan_adapter, "fetch_confirmed_milestones", no_saved)

    res = client.post("/plans/milestones", json=_body(_outcome()))

    assert res.status_code == 200
    assert res.json()["aiSource"] == "llm"


def test_milestones_rule_fallback(client: TestClient, monkeypatch: Any) -> None:
    """LLM 실패 시 룰 폴백(준비→진행→마무리 3단계) + aiSource='rule'."""

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        return RunResult(
            value=kwargs["fallback"](),
            fell_back=True,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)
    res = client.post("/plans/milestones", json=_body(_outcome()))
    assert res.status_code == 200
    body = res.json()
    assert len(body["milestones"]) == 3
    assert body["aiSource"] == "rule"


# ─────────────────────────────────────────────────────────────────────────────
# llm_runs 가 **커밋되는가** — add 만 보면 못 잡는 계측 구멍 (#429)
#
# ⚠️ `llm_budget.record()` 는 `session.add` + `flush` 까지만 하고 **커밋은 호출자 책임**이다
# (`safety/llm_budget.py:288`). Stage A 라우터는 "DB 쓰기가 없다" 고 보고 커밋하지 않아
# 요청이 끝나며 행이 통째로 롤백됐다 — 라이브 실측(2026-08-29) 온보딩 4회에
# `planning/plan_milestones` 행 **0개**. 78ca617 이 커밋을 넣어 고쳤지만 **그 고침을
# 지키는 테스트가 없었다.**
#
# 기존 `test_generate_logs_each_llm_call_to_llm_runs` 도 `cap.added` 만 본다 —
# **add 됐는지만 보고 commit 은 안 본다.** 그래서 이 버그 계열을 원리적으로 못 잡는다.
# ─────────────────────────────────────────────────────────────────────────────


def test_milestones_llm_run_is_committed_not_just_added(
    client: TestClient, monkeypatch: Any
) -> None:
    """Stage A 가 LLM 을 불렀으면 `llm_runs` 행이 **커밋**돼야 한다.

    커밋이 없으면 행은 `add` 는 되지만 요청 종료와 함께 롤백돼, 그 호출이 토큰 예산·
    엔드포인트 호출 상한·원가 리포트 **어디에도 안 잡힌다.**
    """
    _force_provider_timeout(monkeypatch)
    cap = _CapturingSession()
    _use_session(client, cap)

    res = client.post("/plans/milestones", json=_body(_outcome()))
    assert res.status_code == 200

    runs = [o for o in cap.added if isinstance(o, LlmRun)]
    assert len(runs) == 1, "Stage A 는 LLM 1콜이다"
    assert runs[0].prompt_id == "planning/plan_milestones"
    assert runs[0].module == "planning"
    # ⚠️ 이 줄이 이 테스트의 존재 이유다. 위 assert 들은 커밋이 없어도 전부 통과한다.
    assert cap.committed, "llm_runs 행을 add 만 하고 커밋하지 않으면 요청 종료 시 롤백된다"


def test_generate_llm_runs_are_committed(client: TestClient, monkeypatch: Any) -> None:
    """`/plans/generate` 도 같다 — 분해·검토 2행이 **커밋**돼야 한다."""
    _force_provider_timeout(monkeypatch)
    cap = _CapturingSession()
    _use_session(client, cap)

    res = client.post("/plans/generate", json=_body(_outcome()))
    assert res.status_code == 200
    assert len([o for o in cap.added if isinstance(o, LlmRun)]) == 2
    assert cap.committed, "분해·검토 호출이 llm_runs 에 남지 않는다"


def test_milestones_saved_skeleton_records_no_llm_run(client: TestClient, monkeypatch: Any) -> None:
    """저장된 뼈대를 돌려줄 때는 행이 **없는 것이 정상**이다 (ADR-0007 PR-2.5).

    ⚠️ 이것이 `plan_milestones` 행 0개의 **두 번째 원인**이고, 버그가 아니다.
    2주기 이후에는 LLM 을 아예 안 부르므로 이 endpoint 의 콜이 0 이 된다.
    계측 구멍(위)과 이 정상 경로를 **구분하지 못하면 0 을 잘못 읽는다.**
    """
    from reaction_backend.orchestrator import first_plan_adapter
    from reaction_backend.schemas.planning import MilestoneDraft

    async def never(**kwargs: Any) -> RunResult[Any]:
        raise AssertionError("저장된 뼈대가 있으면 LLM 을 부르면 안 된다")

    async def fake_goal_id(*args: Any, **kwargs: Any) -> UUID:
        return uuid4()

    async def fake_saved(*args: Any, **kwargs: Any) -> list[MilestoneDraft]:
        return [MilestoneDraft(title="기초 문법", summary="")]

    monkeypatch.setattr(aiClient, "run", never)
    monkeypatch.setattr(first_plan_adapter, "heaviest_goal_id", fake_goal_id)
    monkeypatch.setattr(first_plan_adapter, "fetch_confirmed_milestones", fake_saved)
    cap = _CapturingSession()
    _use_session(client, cap)

    res = client.post("/plans/milestones", json=_body(_outcome()))

    assert res.status_code == 200
    assert res.json()["aiSource"] == "saved"
    assert [o for o in cap.added if isinstance(o, LlmRun)] == []


def _link_only_outcome() -> InterviewOutcome:
    """참고 자료를 **링크로만** 준 목표 — materials_resolver 가 실제로 열어보는 조건."""
    outcome = _outcome()
    outcome.core_goals[0].materials_note = "https://lecture.example/syllabus"
    return outcome


def test_generate_fetches_the_users_link_exactly_once(client: TestClient, monkeypatch: Any) -> None:
    """한 번의 POST /plans/generate 는 사용자 링크를 **정확히 1회**만 연다 (#226).

    라우트가 tier 게이트로 `validate_inputs` 노드를 통째로 부르고 그래프가 같은 노드를
    진입점으로 또 부르면 요청 1회에 외부 사이트를 2회 두드리고 8s 타임아웃을 2회 태운다
    (#179 가 지적한 20s 예산에 직격). 호출 **횟수**를 세지 않으면 결과는 똑같이 정상이라
    이 회귀가 조용히 지나간다 — 실제로 그렇게 머지됐다.
    """
    from reaction_backend.integrations.web_fetch import fetcher

    calls: list[str] = []

    async def counting_fetch(url: str) -> Any:
        calls.append(url)
        return fetcher.FetchResult("주차별 강의계획: 1주차 개요", None)

    monkeypatch.setattr(fetcher, "fetch_text", counting_fetch)
    monkeypatch.setattr(aiClient, "run", _stub())

    res = client.post("/plans/generate", json=_body(_link_only_outcome()))
    assert res.status_code == 200
    assert calls == ["https://lecture.example/syllabus"], (
        f"링크를 {len(calls)}회 열었다 — 요청 1회당 1회여야 한다: {calls}"
    )


def test_tier_gate_rejects_without_opening_the_link(client: TestClient, monkeypatch: Any) -> None:
    """Focus 한도 초과로 422 를 던질 땐 링크를 **아예 열지 않는다** (#226).

    게이트가 순수 판정이어야 성립한다. 노드를 부르면 버릴 응답을 위해 남의 서버를 두드린다.
    """
    from reaction_backend.integrations.web_fetch import fetcher

    calls: list[str] = []

    async def counting_fetch(url: str) -> Any:
        calls.append(url)
        return fetcher.FetchResult("본문", None)

    monkeypatch.setattr(fetcher, "fetch_text", counting_fetch)

    outcome = _outcome(focus_goals=4)
    outcome.core_goals[0].materials_note = "https://lecture.example/syllabus"
    res = client.post("/plans/generate", json=_body(outcome))

    assert res.status_code == 422
    assert res.json()["code"] == "GOAL_TIER_LIMIT_EXCEEDED"
    assert calls == [], f"422 로 버릴 요청인데 링크를 열었다: {calls}"


def test_milestones_stage_a_receives_the_fetched_material(
    client: TestClient, monkeypatch: Any
) -> None:
    """Stage A(마일스톤)도 링크 본문을 프롬프트로 받는다 (#226).

    계획의 뼈대를 정하는 게 이 단계다. 여기서 자료가 '(없음)' 이면 일반론 마일스톤이 나오고
    Stage B 는 그 위에 '추가·삭제·병합·개명 금지' 로 묶여, 자료를 준 사용자에게 링크를
    무시한 계획이 나간다 — FE 가 #226 을 연 이유.
    """
    from reaction_backend.integrations.web_fetch import fetcher
    from reaction_backend.schemas.planning import MilestoneDraft, MilestonePlan

    seen: dict[str, Any] = {}

    async def stub_fetch(url: str) -> Any:
        return fetcher.FetchResult("주차별 강의계획: 1주차 개요, 2주차 함수", None)

    async def capture_run(**kwargs: Any) -> RunResult[Any]:
        seen.update(kwargs["variables"])
        return RunResult(
            value=MilestonePlan(milestones=[MilestoneDraft(title="t", summary="s")]),
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(fetcher, "fetch_text", stub_fetch)
    monkeypatch.setattr(aiClient, "run", capture_run)

    res = client.post("/plans/milestones", json=_body(_link_only_outcome()))
    assert res.status_code == 200
    assert "주차별 강의계획" in seen["materials"], (
        f"Stage A 가 받은 materials={seen.get('materials')!r}"
    )


def test_milestones_stage_a_falls_back_to_none_when_fetch_fails(
    client: TestClient, monkeypatch: Any
) -> None:
    """링크 열기에 실패하면 Stage A 는 예전처럼 '(없음)' 으로 내려간다 — 회귀 위험 0 (#226)."""
    from reaction_backend.integrations.web_fetch import fetcher
    from reaction_backend.schemas.planning import MilestoneDraft, MilestonePlan

    seen: dict[str, Any] = {}

    async def failing_fetch(url: str) -> Any:
        return fetcher.FetchResult(None, "timeout")

    async def capture_run(**kwargs: Any) -> RunResult[Any]:
        seen.update(kwargs["variables"])
        return RunResult(
            value=MilestonePlan(milestones=[MilestoneDraft(title="t", summary="s")]),
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(fetcher, "fetch_text", failing_fetch)
    monkeypatch.setattr(aiClient, "run", capture_run)

    res = client.post("/plans/milestones", json=_body(_link_only_outcome()))
    assert res.status_code == 200
    assert seen["materials"] == "(없음)"


def test_generate_passes_confirmed_milestones_to_decompose(
    client: TestClient, monkeypatch: Any
) -> None:
    """확정 마일스톤(Stage B)이 decompose 프롬프트 변수 {{milestones}} 로 전달된다."""
    captured: dict[str, Any] = {}

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        value: Any
        if schema is GoalDecomposition:
            captured["milestones"] = kwargs["variables"].get("milestones")
            value = GoalDecomposition(
                goal_nodes=[
                    GoalNodeDraft(
                        node_id="n1",
                        parent_id=None,
                        title="목표0",
                        node_type="root",
                        order_index=0,
                        is_leaf=True,
                    )
                ],
                action_items=[],
                policy_violations=[],
            )
        elif schema is PlanReview:
            value = PlanReview(approved=True, feedback=[])
        else:  # pragma: no cover
            raise AssertionError(f"unexpected schema {schema}")
        return RunResult(
            value=value,
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)
    body = _body(_outcome())
    body["milestones"] = [
        {"title": "기초 문법", "summary": "변수·함수"},
        {"title": "DOM 조작", "summary": ""},
    ]
    res = client.post("/plans/generate", json=body)
    assert res.status_code == 200
    assert "기초 문법" in captured["milestones"]  # 확정 마일스톤이 프롬프트에 실림
    assert "DOM 조작" in captured["milestones"]


def test_generate_warns_when_a_confirmed_milestone_has_no_place(
    client: TestClient, monkeypatch: Any
) -> None:
    """확정 마일스톤이 계획에 안 들어가면 `warnings` 로 알린다 (ADR-0007 §배경 ①).

    여기서는 LLM 이 'DOM 조작' 의 leaf 를 아예 만들지 않은 경우를 쓴다 — 세션 수 상한이
    자르는 경우(단위 테스트에서 검증)와 사용자에게는 같은 일이고, 라우트 레벨에서는
    **고지가 실제로 응답까지 도달하는지**가 관심사다.
    """

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        value: Any
        if schema is GoalDecomposition:
            value = GoalDecomposition(
                goal_nodes=[
                    GoalNodeDraft(
                        node_id="root",
                        parent_id=None,
                        title="목표0",
                        node_type="root",
                        order_index=0,
                        is_leaf=False,
                    ),
                    GoalNodeDraft(
                        node_id="b1",
                        parent_id="root",
                        title="기초 문법",
                        node_type="branch",
                        order_index=0,
                        is_leaf=False,
                    ),
                    GoalNodeDraft(
                        node_id="l1",
                        parent_id="b1",
                        title="변수와 함수 익히기",
                        node_type="leaf",
                        order_index=0,
                        is_leaf=True,
                    ),
                ],
                action_items=[
                    ActionItemDraft(
                        node_id="l1",
                        title="변수와 함수 익히기",
                        estimated_minutes=60,
                        category="study",
                        first_step="s",
                    )
                ],
                policy_violations=[],
            )
        elif schema is PlanReview:
            value = PlanReview(approved=True, feedback=[])
        else:  # pragma: no cover
            raise AssertionError(f"unexpected schema {schema}")
        return RunResult(
            value=value,
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)
    body = _body(_outcome())
    body["milestones"] = [
        {"title": "기초 문법", "summary": "변수·함수"},
        {"title": "DOM 조작", "summary": ""},
    ]
    res = client.post("/plans/generate", json=body)
    assert res.status_code == 200
    warnings = res.json()["warnings"]
    assert any("DOM 조작" in w for w in warnings), warnings
    # 자리를 잡은 마일스톤은 빠졌다고 하지 않는다.
    assert not any("'기초 문법'" in w for w in warnings), warnings


def test_generate_threads_the_milestone_cursor_into_decompose(
    client: TestClient, monkeypatch: Any
) -> None:
    """라우트가 커서를 **재서 그래프에 넘긴다** (ADR-0007 PR-5).

    커서 계산은 라우트에만 있다(그래프는 DB 를 모른다). 실 DB 테스트는
    `completed_milestone_cursor` 를 **직접** 부르고, 그래프 테스트는 커서를 인자로 받으므로,
    **그 사이 배선**이 어느 쪽에도 안 걸린다 — 라우트에서 커서 계산 블록을 통째로 지워도
    전체 스위트가 초록이었다(뮤테이션 확인).

    커서 2 를 심고 마일스톤 4개를 보내면 분해는 **3번째부터** 받아야 한다.
    """
    from reaction_backend.orchestrator import first_plan_adapter

    captured: dict[str, Any] = {}

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        if schema is GoalDecomposition:
            captured.update(kwargs["variables"])
        return RunResult(
            value=kwargs["fallback"](),
            fell_back=True,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    async def fake_goal_id(*args: Any, **kwargs: Any) -> UUID:
        return uuid4()

    async def fake_cursor(*args: Any, **kwargs: Any) -> int:
        return 2  # 앞의 두 개는 이미 끝냈다

    monkeypatch.setattr(aiClient, "run", stub_run)
    monkeypatch.setattr(first_plan_adapter, "heaviest_goal_id", fake_goal_id)
    monkeypatch.setattr(first_plan_adapter, "completed_milestone_cursor", fake_cursor)
    outcome = _outcome()
    outcome.horizon = "2026-12-31"
    body = _body(outcome)
    body["milestones"] = [{"title": f"{i}단계", "summary": ""} for i in range(1, 5)]

    res = client.post("/plans/generate", json=body)

    assert res.status_code == 200
    rendered = captured["milestones"]
    assert "3단계" in rendered, rendered
    for done_or_later in ("1단계", "2단계"):
        assert done_or_later not in rendered, f"커서가 안 먹었다: {rendered}"
    # 끝낸 것은 '다시 시키지 말 것' 으로 실려야 한다.
    assert "1단계" in captured["out_of_cycle"] and "2단계" in captured["out_of_cycle"]


def test_generate_says_later_milestones_are_for_the_next_cycle_not_missing(
    client: TestClient, monkeypatch: Any
) -> None:
    """이번 주기 밖 마일스톤은 **누락이 아니라 순서**로 말한다 (ADR-0007 PR-5 + 라이브 8/29).

    ADR-0007 PR-5 는 "뒤쪽 마일스톤을 누락으로 알리지 않는다" 로 정했다 — 마감이 멀면 앞쪽만
    세션화하는 게 정상이라, 누락 고지가 전체 목록을 보면 **매 계획마다 "빠졌어요" 가 뜨기**
    때문이다(#307 이 잡으려던 건 "세션 수 상한에 잘려 조용히 사라진 것" 이다).

    그 결론(누락 어휘 금지)은 그대로 두되, **아예 침묵하는 것**은 되돌린다. 라이브 실측
    (2026-08-29)에서 사용자가 확인한 마지막 마일스톤이 트리에 아예 없는데 `warnings` 는
    날짜 이야기만 해서, 자기가 승인한 단계가 어디 갔는지 알 방법이 없었다. 그래서 이미
    있던 `out_of_cycle_notice`("이어지는 주기에서 받아요")를 이 경로에도 쓴다.

    라우트를 통째로 태운다 — 헬퍼를 직접 부르는 테스트만으로는 배선을 되돌려도 초록이다.
    """

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        value: Any
        if schema is GoalDecomposition:
            value = GoalDecomposition(
                goal_nodes=[
                    GoalNodeDraft(
                        node_id="root",
                        parent_id=None,
                        title="목표0",
                        node_type="root",
                        order_index=0,
                        is_leaf=False,
                    ),
                    GoalNodeDraft(
                        node_id="b1",
                        parent_id="root",
                        title="1단계",
                        node_type="branch",
                        order_index=0,
                        is_leaf=False,
                    ),
                    GoalNodeDraft(
                        node_id="l1",
                        parent_id="b1",
                        title="1단계 세션",
                        node_type="leaf",
                        order_index=0,
                        is_leaf=True,
                    ),
                ],
                action_items=[
                    ActionItemDraft(
                        node_id="l1",
                        title="1단계 세션",
                        estimated_minutes=60,
                        category="study",
                        first_step="자료 열기",
                    )
                ],
            )
        elif schema is PlanReview:
            value = PlanReview(approved=True, feedback=[])
        else:  # pragma: no cover
            raise AssertionError(f"unexpected schema {schema}")
        return RunResult(
            value=value,
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)
    outcome = _outcome()
    outcome.horizon = "2026-12-31"  # target_date(2026-06-22) 로부터 약 27주
    body = _body(outcome)
    body["milestones"] = [
        {"title": "1단계", "summary": ""},
        {"title": "2단계", "summary": ""},
        {"title": "3단계", "summary": ""},
        {"title": "4단계", "summary": ""},
    ]

    res = client.post("/plans/generate", json=body)

    assert res.status_code == 200
    warnings = res.json()["warnings"]

    # 누락 어휘(`missing_milestones_notice`)는 여전히 금지 — 이게 PR-5 가 막으려던 것이다.
    assert not any("아직 넣지 않은" in w for w in warnings), warnings

    # 대신 "이어지는 주기가 받는다" 로 이름을 불러 준다.
    forward = [w for w in warnings if "이어지는 주기에서 받아요" in w]
    assert len(forward) == 1, warnings
    for later in ("2단계", "3단계", "4단계"):
        assert later in forward[0], f"{later} 를 말해주지 않았다: {forward[0]}"


# ─────────────────────────────────────────────────────────────────────────────
# 만다라 유래 목표 2주 지평 (ADR-0008 §3, §8 "D")
# ─────────────────────────────────────────────────────────────────────────────


class _PromotedTitleSession:
    """`fetch_promoted_goal_titles_for_user` 의 `select(Goal)...` 만 응답하는 최소 fake.

    `_FakeSession.execute` 는 항상 빈 결과라(다른 mandala 관련 테스트와 같은 HTTP 경계
    한계) `_max_plan_weeks` 를 HTTP 클라이언트로는 검증할 수 없다 — 여기서 함수를 직접
    호출해 그 판정 로직만 확인한다.
    """

    def __init__(self, titles: list[str]) -> None:
        self._titles = titles

    async def execute(self, stmt: Any) -> Any:  # noqa: ARG002
        titles = self._titles

        class _Result:
            def scalars(self) -> _Result:
                return self

            def all(self) -> list[Goal]:
                out = []
                for t in titles:
                    g = Goal()
                    g.title = t
                    out.append(g)
                return out

        return _Result()


async def test_max_plan_weeks_is_two_when_heaviest_title_is_a_promoted_axis() -> None:
    outcome = _outcome()  # heaviest.title == "focus0"
    session = _PromotedTitleSession(["focus0"])

    weeks = await _max_plan_weeks(session, uuid4(), outcome)  # type: ignore[arg-type]

    assert weeks == 2


async def test_max_plan_weeks_is_four_when_heaviest_title_is_not_promoted() -> None:
    outcome = _outcome()  # heaviest.title == "focus0"
    session = _PromotedTitleSession(["다른 축 목표"])  # 승격 목록에 없음

    weeks = await _max_plan_weeks(session, uuid4(), outcome)  # type: ignore[arg-type]

    assert weeks == 4


async def test_max_plan_weeks_is_four_when_user_has_no_promoted_goals() -> None:
    outcome = _outcome()
    session = _PromotedTitleSession([])  # 승격한 축 자체가 없음

    weeks = await _max_plan_weeks(session, uuid4(), outcome)  # type: ignore[arg-type]

    assert weeks == 4


def test_generate_uses_two_week_horizon_for_mandala_derived_goal(
    client: TestClient, monkeypatch: Any
) -> None:
    """전체 라우트 경로 — heaviest 가 승격된 축이면 decompose 프롬프트가 2주 기준을 받는다.

    `_FakeSession.execute` 가 항상 빈 결과라 `_max_plan_weeks` 자체는 이 경로에서 늘
    4주로 떨어진다(위 단위 테스트가 그 판정 로직을 커버) — 여기서는 `max_plan_weeks`
    가 그래프까지 무사히 전달돼 `horizon_weeks` 프롬프트 변수에 실제로 반영되는지,
    배선이 끊기지 않았는지를 확인한다.
    """
    captured: dict[str, Any] = {}

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        value: Any
        if schema is GoalDecomposition:
            captured["horizon_weeks"] = kwargs["variables"].get("horizon_weeks")
            value = GoalDecomposition(
                goal_nodes=[
                    GoalNodeDraft(
                        node_id="n1",
                        parent_id=None,
                        title="목표0",
                        node_type="root",
                        order_index=0,
                        is_leaf=True,
                    )
                ],
                action_items=[],
                policy_violations=[],
            )
        elif schema is PlanReview:
            value = PlanReview(approved=True, feedback=[])
        else:  # pragma: no cover
            raise AssertionError(f"unexpected schema {schema}")
        return RunResult(
            value=value,
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)
    outcome = _outcome()
    outcome = outcome.model_copy(update={"horizon": "2026-09-30"})
    res = client.post("/plans/generate", json=_body(outcome, target_date="2026-07-28"))

    assert res.status_code == 200
    # fake session 한계로 이 goal 은 승격 목록에 안 걸려 기본 4주가 나온다 — 배선(즉
    # max_plan_weeks 가 그래프까지 끊기지 않고 전달됨) 자체를 확인하는 게 이 테스트의 목적.
    assert captured["horizon_weeks"] == "4"


def test_milestones_endpoint_commits_the_llm_run_row(client: TestClient, monkeypatch: Any) -> None:
    """Stage A 도 `llm_runs` 를 **커밋**한다 — 안 하면 호출이 계측에서 통째로 사라진다.

    회귀(라이브 실측 2026-08-29): 온보딩 4회를 돌렸는데 `planning/plan_milestones` 행이
    DB 에 0개였다. `record_run` 은 `session.add` 만 하고 커밋은 호출자 책임인데, 이 라우터가
    "DB 쓰기 없음" 이라 보고 커밋하지 않아 요청 종료와 함께 행이 롤백됐다. 그러면 이 호출이
    토큰 예산·엔드포인트 호출 상한(#325/#370)·원가 리포트 어디에도 안 잡힌다.
    """
    _force_provider_timeout(monkeypatch)
    cap = _CapturingSession()
    _use_session(client, cap)

    res = client.post("/plans/milestones", json=_body(_outcome()))
    assert res.status_code == 200

    runs = [o for o in cap.added if isinstance(o, LlmRun)]
    assert [r.prompt_id for r in runs] == ["planning/plan_milestones"]
    assert cap.committed, "llm_runs 행을 add 만 하고 커밋하지 않으면 요청 끝에 롤백된다"


# ─────────────────────────────────────────────────────────────────────────────
# #398 — `goalId` 로 계획 대상 목표를 명시한다.
#
# `GET /reviews/weekly` 의 `nextCycleProposals` 는 `goalId` 를 주는데, 승인 경로로 안내된
# `POST /plans/generate` 는 목표를 못 받고 **최근 완료 인터뷰**를 재투영했다. 목표를 여러 개
# 굴리면 목표 A 의 제안을 열었는데 목표 B 의 계획이 생성·승인될 수 있었다.
# ─────────────────────────────────────────────────────────────────────────────


def _seed_goal(
    repo: FakeGoalRepo,
    *,
    title: str,
    user_id: UUID = DEMO_USER_UUID,
    status: str = "active",
    tier: str = "focus",
    deadline: date | None = None,
) -> Goal:
    g = Goal()
    g.id = uuid4()
    g.user_id = user_id
    g.title = title
    g.category = "study"
    g.goal_tier = tier
    g.status = status
    g.deadline = deadline
    g.archived_at = None
    repo._items[g.id] = g
    return g


def _multi_goal_outcome() -> InterviewOutcome:
    """heaviest 가 **B** 인 outcome — 가장 최근 인터뷰가 B 를 골랐다는 상황."""
    base = _outcome()
    return base.model_copy(
        update={
            "core_goals": [
                GoalCandidate(
                    title="목표B",
                    category="study",
                    is_heaviest=True,
                    tentative_tier="focus",
                    confidence=0.9,
                ),
                GoalCandidate(
                    title="목표A",
                    category="study",
                    weekly_hours=6,
                    session_length_min=50,
                    tentative_tier="focus",
                    confidence=0.8,
                ),
            ]
        }
    )


def test_generate_with_goal_id_plans_that_goal_not_the_latest_interview_heaviest(
    client: TestClient, fake_goal_repo: FakeGoalRepo, monkeypatch: Any
) -> None:
    """#398 회귀 — 최근 인터뷰는 B 를 골랐는데 `goalId=A` 를 주면 **A** 로 계획이 선다.

    이게 이 이슈의 핵심 재현이다: 제안 카드가 A 의 `goalId` 를 주는데 계획은 B 가 서던 것.
    """
    goal_a = _seed_goal(fake_goal_repo, title="목표A")
    captured: dict[str, Any] = {}

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        if kwargs["schema"] is GoalDecomposition:
            captured["variables"] = kwargs.get("variables", {})
        return await _stub()(**kwargs)

    monkeypatch.setattr(aiClient, "run", stub_run)

    body = _body(_multi_goal_outcome())
    body["goalId"] = f"goal_{goal_a.id}"
    res = client.post("/plans/generate", json=body)

    assert res.status_code == 200
    goal_title = captured["variables"]["goal_title"]
    assert goal_title == "목표A", (
        f"heaviest 가 갈아끼워지지 않았다 — 분해가 받은 목표: {goal_title}"
    )


def test_generate_with_goal_id_keeps_the_slots_the_user_answered_for_that_goal(
    client: TestClient, fake_goal_repo: FakeGoalRepo, monkeypatch: Any
) -> None:
    """A 에 대해 이미 답해 둔 슬롯(주당 시간·세션 길이)은 버리지 않는다.

    제목이 같은 `core_goals` 항목을 template 으로 삼는다 — 안 그러면 목표를 지정했다는
    이유만으로 사용자가 인터뷰에서 답한 값이 사라진다.
    """
    goal_a = _seed_goal(fake_goal_repo, title="목표A")
    captured: dict[str, Any] = {}

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        if kwargs["schema"] is GoalDecomposition:
            captured["variables"] = kwargs.get("variables", {})
        return await _stub()(**kwargs)

    monkeypatch.setattr(aiClient, "run", stub_run)

    body = _body(_multi_goal_outcome())
    body["goalId"] = f"goal_{goal_a.id}"
    res = client.post("/plans/generate", json=body)

    assert res.status_code == 200
    # 프롬프트 변수는 `first_plan_adapter` 가 heaviest 에서 파생한다 — template 을 안 쓰면
    # A 에 대한 답이 사라져 "(미입력)" 이 된다.
    assert captured["variables"]["weekly_hours"] == "6시간"


def test_generate_without_goal_id_is_unchanged(
    client: TestClient, fake_goal_repo: FakeGoalRepo, monkeypatch: Any
) -> None:
    """`goalId` 를 안 주면 종전 그대로 — 최근 인터뷰의 heaviest(B)를 재투영한다.

    additive 임을 못 박는다. 이게 깨지면 FE 의 기존 빈 본문 호출이 전부 달라진다.
    """
    _seed_goal(fake_goal_repo, title="목표A")
    captured: dict[str, Any] = {}

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        if kwargs["schema"] is GoalDecomposition:
            captured["variables"] = kwargs.get("variables", {})
        return await _stub()(**kwargs)

    monkeypatch.setattr(aiClient, "run", stub_run)

    res = client.post("/plans/generate", json=_body(_multi_goal_outcome()))

    assert res.status_code == 200
    assert captured["variables"]["goal_title"] == "목표B"


def test_generate_rejects_another_users_goal(
    client: TestClient, fake_goal_repo: FakeGoalRepo, monkeypatch: Any
) -> None:
    """남의 목표는 404 — "있지만 권한 없음" 을 돌려주면 존재 여부가 새어 나간다."""
    monkeypatch.setattr(aiClient, "run", _stub())
    theirs = _seed_goal(fake_goal_repo, title="남의 목표", user_id=uuid4())

    body = _body(_outcome())
    body["goalId"] = f"goal_{theirs.id}"
    res = client.post("/plans/generate", json=body)

    assert res.status_code == 404
    assert res.json()["code"] == "GOAL_NOT_FOUND"


def test_generate_rejects_archived_goal(
    client: TestClient, fake_goal_repo: FakeGoalRepo, monkeypatch: Any
) -> None:
    """보관(soft delete)된 목표도 404 — `get_by_id` 가 `archived_at IS NULL` 을 함께 건다."""
    monkeypatch.setattr(aiClient, "run", _stub())
    archived = _seed_goal(fake_goal_repo, title="치운 목표")
    archived.archived_at = now_kst()

    body = _body(_outcome())
    body["goalId"] = f"goal_{archived.id}"
    res = client.post("/plans/generate", json=body)

    assert res.status_code == 404


def test_generate_rejects_completed_goal_with_422_not_404(
    client: TestClient, fake_goal_repo: FakeGoalRepo, monkeypatch: Any
) -> None:
    """완료한 목표는 422 — 목표는 실제로 있고 화면에도 보이므로 "없다" 는 거짓말이 된다."""
    monkeypatch.setattr(aiClient, "run", _stub())
    done = _seed_goal(fake_goal_repo, title="끝낸 목표", status="completed")

    body = _body(_outcome())
    body["goalId"] = f"goal_{done.id}"
    res = client.post("/plans/generate", json=body)

    assert res.status_code == 422
    assert res.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_generate_rejects_malformed_goal_id(client: TestClient, monkeypatch: Any) -> None:
    """`goal_` 접두사 규약을 안 지킨 값은 404 — `routes/goals.py` 와 같은 규약."""
    monkeypatch.setattr(aiClient, "run", _stub())

    body = _body(_outcome())
    body["goalId"] = "not-a-goal-id"
    res = client.post("/plans/generate", json=body)

    assert res.status_code == 404


async def test_goal_id_seed_still_gets_the_two_week_mandala_cap() -> None:
    """만다라 승격 목표의 2주 지평이 그대로 걸린다 (#398 완료 조건).

    `_max_plan_weeks` 는 heaviest **제목**으로 판정하므로, `goal_cycle.seed_outcome` 이
    제목을 그 목표로 갈아끼우면 판정이 자동으로 따라온다 — 새 규칙을 넣지 않는다.
    """
    promoted = Goal()
    promoted.id = uuid4()
    promoted.title = "승격된 축"
    promoted.category = "other"
    promoted.goal_tier = "focus"
    promoted.deadline = None

    seeded = goal_cycle.seed_outcome(base=_outcome(), goal=promoted)
    assert seeded.core_goals[0].title == "승격된 축"

    weeks = await _max_plan_weeks(
        _PromotedTitleSession(["승격된 축"]),  # type: ignore[arg-type]
        uuid4(),
        seeded,
    )
    assert weeks == 2
