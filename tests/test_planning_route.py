"""Planning route (#32) — POST /plans/generate 실배선 검증.

ADR-0005 §7.3 패턴: aiClient.run 만 stub (Gemini 미호출). 라우터 → first_plan 그래프
(decompose LLM → schedule 룰 → review LLM) → Draft 응답 경로를 HTTP 레벨로 검증한다.

- 정상 흐름: Draft 응답 + 룰 스케줄러가 action_item 을 가용 시간에 배치.
- 실패 흐름: Focus 한도(≤3) 초과 시 LLM 분해 전 422 GOAL_TIER_LIMIT_EXCEEDED.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from reaction_backend.config import get_settings
from reaction_backend.db.models.interview_session import InterviewSession as InterviewSessionRow
from reaction_backend.db.models.llm_run import LlmRun
from reaction_backend.db.session import get_db
from reaction_backend.llm import RunResult, aiClient
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
from tests.conftest import DEMO_USER_UUID, FakeInterviewRepo, _FakeSession

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
        preferences=PreferenceProfile(recovery_tone="담백", rest_ok=True, downscope_ok=True),
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


def _block_dict(node_id: str, hour: int, minute: int = 0, dur: int = 30) -> dict[str, Any]:
    """ScheduledBlockPreview camelCase JSON (approve 요청 blocks 용)."""
    start = datetime(2026, 6, 22, hour, minute, tzinfo=KST)
    end = start + timedelta(minutes=dur)
    return ScheduledBlockPreview(
        start=start, end=end, title="작업", category="study", origin="goal", origin_id=node_id
    ).model_dump(by_alias=True, mode="json")


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
    assert body["planId"].startswith("plan_")
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
    """outcome / interviewSessionId 둘 다 없으면 422 COMMON_VALIDATION_ERROR."""
    monkeypatch.setattr(aiClient, "run", _stub())
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
# approve — SAVING (가드 트랜잭션 + 롤백)
# ─────────────────────────────────────────────────────────────────────────────


def test_approve_persists_within_guarded_transaction(client: TestClient) -> None:
    """승인 → 가용 시간 블록 영속화 + commit, is_draft=false."""
    cap = _CapturingSession()
    _use_session(client, cap)

    action = ActionItemDraft(
        node_id="n1", title="작업", estimated_minutes=30, category="study", first_step="시작"
    )
    body = {
        "outcome": _outcome().model_dump(by_alias=True, mode="json"),
        "actionItems": [action.model_dump(by_alias=True)],
        "blocks": [_block_dict("n1", 10)],  # 10:00 — 활동시간 내(09~23)
        "targetDate": "2026-06-22",
    }
    res = client.post("/plans/plan_demo/approve", json=body)
    assert res.status_code == 200
    j = res.json()
    assert j["isDraft"] is False
    assert j["planId"] == "plan_demo"
    assert j["activatedActionItems"] == 1
    assert j["activatedBlocks"] == 1
    assert cap.committed is True
    assert cap.rolled_back is False


def test_approve_policy_violation_rolls_back(client: TestClient) -> None:
    """수면(23~09) 시간과 겹치는 블록 → 가드가 롤백 + 422 PLAN_POLICY_VIOLATION."""
    cap = _CapturingSession()
    _use_session(client, cap)

    action = ActionItemDraft(
        node_id="n1", title="작업", estimated_minutes=30, category="study", first_step="시작"
    )
    body = {
        "outcome": _outcome().model_dump(by_alias=True, mode="json"),
        "actionItems": [action.model_dump(by_alias=True)],
        "blocks": [_block_dict("n1", 2)],  # 02:00 — 수면 시간 침범
        "targetDate": "2026-06-22",
    }
    res = client.post("/plans/plan_demo/approve", json=body)
    assert res.status_code == 422
    assert res.json()["code"] == "PLAN_POLICY_VIOLATION"
    assert cap.rolled_back is True
    assert cap.committed is False


def _approve_body() -> dict[str, Any]:
    action = ActionItemDraft(
        node_id="n1", title="작업", estimated_minutes=30, category="study", first_step="시작"
    )
    return {
        "outcome": _outcome().model_dump(by_alias=True, mode="json"),
        "actionItems": [action.model_dump(by_alias=True)],
        "blocks": [_block_dict("n1", 10)],
        "targetDate": "2026-06-22",
    }


def test_approve_advances_onboarding_first_plan_to_notifications(
    client: TestClient, demo_user_orm: Any
) -> None:
    """승인 → onboarding ONBOARDING_FIRST_PLAN → ONBOARDING_NOTIFICATIONS (Issue #17 규약)."""
    demo_user_orm.onboarding_state = "ONBOARDING_FIRST_PLAN"

    res = client.post("/plans/plan_demo/approve", json=_approve_body())
    assert res.status_code == 200
    assert demo_user_orm.onboarding_state == "ONBOARDING_NOTIFICATIONS"


def test_approve_does_not_regress_onboarding_when_active(
    client: TestClient, demo_user_orm: Any
) -> None:
    """이미 ACTIVE 인 사용자(재계획 등)는 승인해도 onboarding 후퇴 없음 (멱등)."""
    demo_user_orm.onboarding_state = "ACTIVE"

    res = client.post("/plans/plan_demo/approve", json=_approve_body())
    assert res.status_code == 200
    assert demo_user_orm.onboarding_state == "ACTIVE"
