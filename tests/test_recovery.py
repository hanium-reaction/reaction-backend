"""Recovery — #20-A 수직 슬라이스 (api-contract §12).

`GEMINI_API_KEY` 가 빈 상태이므로 `aiClient.run` 은 자동으로 룰 fallback 분기
→ 카드 문구는 카탈로그 템플릿, `aiSource="rule"`.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from reaction_backend.orchestrator.recovery import render_template, select_strategies
from tests.conftest import (
    DEMO_USER_UUID,
    FakeActionItemRepo,
    FakeRecoveryRepo,
    default_recovery_strategies,
)


def _seed_failed_execution(
    recovery_repo: FakeRecoveryRepo,
    action_repo: FakeActionItemRepo,
    *,
    completion_status: str = "failed",
    failure_tags: list[str] | None = None,
    title: str = "GROUP BY 실습",
) -> str:
    """실패한 실행 1건 시드 → `exec_<uuid>` ID 반환."""
    action = _seed_action(action_repo, title=title)
    execution = recovery_repo.register_execution(
        user_id=DEMO_USER_UUID,
        action_item_id=action.id,
        completion_status=completion_status,
        failure_tags=failure_tags or ["AMBIGUITY"],
    )
    return f"exec_{execution.id}"


def _seed_action(action_repo: FakeActionItemRepo, *, title: str) -> Any:
    from reaction_backend.db.models.action_item import ActionItem

    a = ActionItem()
    a.id = uuid4()
    a.user_id = DEMO_USER_UUID
    a.title = title
    a.target_date = date(2026, 6, 5)
    a.category = "study"
    a.source = "manual"
    a.status = "failed"
    a.priority = 3
    a.estimated_minutes = 60
    a.why_now = None
    a.first_step = None
    a.goal_id = None
    a.archived_at = None
    action_repo.seed(a)
    return a


def _generate(client: TestClient, execution_id: str) -> Any:
    return client.post(
        "/recovery/proposals/generate",
        json={"executionId": execution_id},
    )


def _decide(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post(
        "/recovery/decisions",
        json=body,
        headers={"Idempotency-Key": f"test-{uuid4()}"},
    )


# ───────────────────────── proposals/generate ─────────────────────────


def test_generate_returns_2_to_4_cards(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY", "CONFLICT"]
    )
    resp = _generate(client, exec_id)
    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert body["executionId"] == exec_id
    assert 2 <= len(body["cards"]) <= 4
    # Draft Layer 강제 (ADR-0005 §7.2)
    assert body["isDraft"] is True
    # LLM 키 없음 → 룰 fallback
    assert body["aiSource"] == "rule"


def test_generate_max_one_card_per_group(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    # FATIGUE 는 DOWNSCOPE_DEFAULT 와 ACTIVE_RECOVERY 둘 다 트리거 — 그룹별 1장 보장 확인
    exec_id = _seed_failed_execution(
        fake_recovery_repo,
        fake_action_item_repo,
        failure_tags=["FATIGUE", "PLAN_TOO_BIG", "LOW_ENERGY"],
    )
    body = _generate(client, exec_id).json()
    groups = [c["optionGroup"] for c in body["cards"]]
    assert len(groups) == len(set(groups)), f"같은 그룹 중복 노출: {groups}"


def test_generate_ambiguity_maps_to_nano_step(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY"]
    )
    body = _generate(client, exec_id).json()
    top = body["cards"][0]
    assert top["strategyType"] == "NANO_STEP"
    assert top["optionGroup"] == "DOWNSCOPE"
    assert top["triggerTag"] == "AMBIGUITY"
    # 템플릿 변수 {first_step} 치환 — 원본 카드 제목이 문구에 포함
    assert "GROUP BY 실습" in top["suggestedActionText"]


def test_generate_no_tags_still_pads_to_min_cards(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """태그가 없어도 항상 최소 2장 — '빈 화면' 금지."""
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo, failure_tags=[])
    body = _generate(client, exec_id).json()
    assert len(body["cards"]) >= 2


def test_generate_is_idempotent_while_pending(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    first = _generate(client, exec_id).json()
    second = _generate(client, exec_id).json()
    assert [c["attemptId"] for c in first["cards"]] == [c["attemptId"] for c in second["cards"]]


def test_generate_404_unknown_execution(client: TestClient) -> None:
    resp = _generate(client, f"exec_{uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "RECOVERY_EXECUTION_NOT_FOUND"


def test_generate_422_not_eligible(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, completion_status="done"
    )
    resp = _generate(client, exec_id)
    assert resp.status_code == 422
    assert resp.json()["code"] == "RECOVERY_NOT_ELIGIBLE"


# ───────────────────────── decisions ─────────────────────────


def test_decisions_requires_idempotency_key(client: TestClient) -> None:
    resp = client.post(
        "/recovery/decisions",
        json={"executionId": f"exec_{uuid4()}", "decision": "skipped"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_decision_accept_downscope_creates_action_and_rejects_siblings(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["AMBIGUITY", "CONFLICT"]
    )
    cards = _generate(client, exec_id).json()["cards"]
    accepted = next(c for c in cards if c["optionGroup"] == "DOWNSCOPE")

    resp = _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "accepted",
            "acceptedAttemptId": accepted["attemptId"],
        },
    )
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["isDraft"] is False
    assert body["acceptedAttemptId"] == accepted["attemptId"]
    assert len(body["rejectedAttemptIds"]) == len(cards) - 1
    # DOWNSCOPE 수락 → 새 ActionItem(source=recovery_downscope) 생성
    assert body["resultingActionItemId"] is not None
    new_actions = [
        a for a in fake_action_item_repo._items.values() if a.source == "recovery_downscope"
    ]
    assert len(new_actions) == 1
    # 원본 카드 status 불변 (AGENTS.md §2 — Resilience 지표 전제)
    original = next(a for a in fake_action_item_repo._items.values() if a.source == "manual")
    assert original.status == "failed"
    # 혈통 기록
    assert new_actions[0].parent_action_item_id == original.id


def test_decision_accept_reschedule_creates_no_action(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(
        fake_recovery_repo, fake_action_item_repo, failure_tags=["CONFLICT"]
    )
    cards = _generate(client, exec_id).json()["cards"]
    reschedule = next(c for c in cards if c["optionGroup"] == "RESCHEDULE")
    body = _decide(
        client,
        {
            "executionId": exec_id,
            "decision": "accepted",
            "acceptedAttemptId": reschedule["attemptId"],
        },
    ).json()
    # RESCHEDULE 은 새 ActionItem 없음 (§5.16 — replan S20 에서 scheduled_blocks 처리)
    assert body["resultingActionItemId"] is None


def test_decision_skip_all(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    cards = _generate(client, exec_id).json()["cards"]
    body = _decide(
        client,
        {"executionId": exec_id, "decision": "skipped", "decisionReason": "오늘은 쉬기"},
    ).json()
    assert body["acceptedAttemptId"] is None
    assert len(body["skippedAttemptIds"]) == len(cards)


def test_decision_conflict_when_already_decided(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    _generate(client, exec_id)
    _decide(client, {"executionId": exec_id, "decision": "skipped"})
    resp = _decide(client, {"executionId": exec_id, "decision": "skipped"})
    assert resp.status_code == 409
    assert resp.json()["code"] == "RECOVERY_ALREADY_DECIDED"


def test_decision_accept_requires_attempt_id(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    exec_id = _seed_failed_execution(fake_recovery_repo, fake_action_item_repo)
    _generate(client, exec_id)
    resp = _decide(client, {"executionId": exec_id, "decision": "accepted"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


# ───────────────────────── replan (#20-B 후속) ─────────────────────────


def test_replan_endpoints_still_501(client: TestClient) -> None:
    assert client.get(f"/replan/exec_{uuid4()}").status_code == 501


# ───────────────────────── 룰 엔진 단위 테스트 ─────────────────────────


def test_select_strategies_caps_at_max() -> None:
    strategies = default_recovery_strategies()
    # 4그룹 모두 트리거 + 패딩 → 최대 4장
    tags = ["AMBIGUITY", "CONFLICT", "PRIORITY_SHIFT", "DISTRACTION", "EMERGENCY"]
    cards = select_strategies(tags, strategies)
    assert len(cards) <= 4
    groups = [c.option_group for c in cards]
    assert len(groups) == len(set(groups))


def test_select_strategies_score_beats_priority() -> None:
    strategies = default_recovery_strategies()
    # FATIGUE+LOW_ENERGY → ACTIVE_RECOVERY(2점) 가 RESCHEDULE_DEFAULT(0점) 대신 선택
    cards = select_strategies(["FATIGUE", "LOW_ENERGY"], strategies)
    reschedule_cards = [c for c in cards if c.option_group == "RESCHEDULE"]
    assert reschedule_cards and reschedule_cards[0].strategy_type == "ACTIVE_RECOVERY"


def test_render_template_missing_var_is_safe() -> None:
    assert render_template("딱 5분만 {first_step}", {}) == "딱 5분만"
    assert (
        render_template("{suspended_step} 부터 다시", {"suspended_step": "ERD 검토"})
        == "ERD 검토 부터 다시"
    )
