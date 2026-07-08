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

from reaction_backend.config import get_settings
from reaction_backend.db.models.interview_session import InterviewSession as InterviewSessionRow
from reaction_backend.db.models.llm_run import LlmRun
from reaction_backend.db.models.plan_draft import PlanDraft
from reaction_backend.db.session import get_db
from reaction_backend.llm import RunResult, aiClient
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
