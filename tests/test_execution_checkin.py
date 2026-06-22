"""Today 실행 쓰기 (#19-B) — start / check-in / failure-tags (api-contract §10·§11).

마지막 E2E 테스트는 중간발표 데모 루프 그 자체:
start → 못함 체크인 → 실패 사유(AMBIGUITY) → Recovery 카드(NANO_STEP) → 수락 → 새 카드.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from tests.conftest import (
    DEMO_USER_UUID,
    FakeActionItemRepo,
    FakeExecutionRepo,
)


def _seed_action(
    action_repo: FakeActionItemRepo,
    *,
    title: str = "GROUP BY 실습",
    estimated_minutes: int = 60,
) -> Any:
    from reaction_backend.db.models.action_item import ActionItem

    a = ActionItem()
    a.id = uuid4()
    a.user_id = DEMO_USER_UUID
    a.title = title
    a.target_date = date(2026, 6, 5)
    a.category = "study"
    a.source = "manual"
    a.status = "planned"
    a.priority = 3
    a.estimated_minutes = estimated_minutes
    a.why_now = None
    a.first_step = None
    a.goal_id = None
    a.archived_at = None
    action_repo.seed(a)
    return a


def _start(client: TestClient, action_id: str) -> Any:
    return client.post(f"/today/actions/{action_id}/start")


def _check_in(client: TestClient, execution_id: str, status: str = "failed", **extra: Any) -> Any:
    return client.post(
        "/today/check-ins",
        json={"executionId": execution_id, "completionStatus": status, **extra},
    )


# ───────────────────────── start ─────────────────────────


def test_start_creates_execution_and_adhoc_block(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    action = _seed_action(fake_action_item_repo)
    resp = _start(client, f"action_{action.id}")
    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert body["executionId"].startswith("exec_")
    assert body["completionStatus"] == "in_progress"
    # 블록이 없었으므로 즉석 블록 생성 (source=user_edit, started)
    blocks = list(fake_execution_repo._blocks.values())
    assert len(blocks) == 1
    assert blocks[0].source == "user_edit"
    assert blocks[0].block_status == "started"
    # 카드 상태 전이 — 실행 시작은 execution 레이어 책임
    assert action.status == "in_progress"


def test_start_conflict_when_already_active(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    action = _seed_action(fake_action_item_repo)
    _start(client, f"action_{action.id}")
    resp = _start(client, f"action_{action.id}")
    assert resp.status_code == 409
    assert resp.json()["code"] == "TODAY_EXECUTION_ALREADY_ACTIVE"


def test_start_404_unknown_action(client: TestClient) -> None:
    resp = _start(client, f"action_{uuid4()}")
    assert resp.status_code == 404


# ───────────────────────── check-in ─────────────────────────


def test_check_in_done_finishes_execution(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    resp = _check_in(client, exec_id, "done", userRating=4)
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["completionStatus"] == "done"
    assert body["needsFailureTags"] is False
    assert body["actualDurationMinutes"] is not None
    # 카드 상태 + 블록 종결
    assert action.status == "done"
    assert all(b.block_status == "finished" for b in fake_execution_repo._blocks.values())


def test_check_in_failed_needs_failure_tags(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    body = _check_in(client, exec_id, "failed").json()
    assert body["needsFailureTags"] is True
    assert action.status == "failed"


def test_check_in_encrypts_feedback(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _check_in(client, exec_id, "partial_done", userFeedback="절반쯤에서 막혔다")
    execution = next(iter(fake_execution_repo._executions.values()))
    assert execution.user_feedback_encrypted is not None
    assert execution.user_feedback_encrypted != "절반쯤에서 막혔다"


def test_check_in_conflict_when_already_done(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _check_in(client, exec_id, "done")
    resp = _check_in(client, exec_id, "failed")
    assert resp.status_code == 409
    assert resp.json()["code"] == "TODAY_ALREADY_CHECKED_IN"


def test_check_in_404_unknown_execution(client: TestClient) -> None:
    resp = _check_in(client, f"exec_{uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "TODAY_EXECUTION_NOT_FOUND"


# ───────────────────────── reflection failure-tags ─────────────────────────


def test_failure_tags_master_returns_13(client: TestClient) -> None:
    resp = client.get("/reflection/failure-tags")
    assert resp.status_code == 200
    tags = resp.json()
    assert len(tags) == 13
    assert tags[0]["tagCode"] == "TIME_SHORTAGE"  # sort_order 순


def test_tag_failure_reasons_success(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _check_in(client, exec_id, "failed")
    resp = client.post(
        f"/reflection/failure-tags/{exec_id}",
        json={"tagCodes": ["AMBIGUITY", "FATIGUE"], "memo": "어디서 시작할지 몰랐다"},
    )
    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert body["tagCodes"] == ["AMBIGUITY", "FATIGUE"]
    assert body["hasMemo"] is True
    # 메모는 평문 저장 금지
    assert fake_execution_repo._last_memo_encrypted != "어디서 시작할지 몰랐다"


def test_tag_rejects_more_than_two(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _check_in(client, exec_id, "failed")
    resp = client.post(
        f"/reflection/failure-tags/{exec_id}",
        json={"tagCodes": ["AMBIGUITY", "FATIGUE", "CONFLICT"]},
    )
    assert resp.status_code == 422  # pydantic max_length=2


def test_tag_rejects_invalid_code(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _check_in(client, exec_id, "failed")
    resp = client.post(f"/reflection/failure-tags/{exec_id}", json={"tagCodes": ["BOGUS"]})
    assert resp.status_code == 422
    assert resp.json()["code"] == "REFLECT_INVALID_TAG"


def test_tag_conflict_when_already_tagged(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _check_in(client, exec_id, "failed")
    client.post(f"/reflection/failure-tags/{exec_id}", json={"tagCodes": ["AMBIGUITY"]})
    resp = client.post(f"/reflection/failure-tags/{exec_id}", json={"tagCodes": ["FATIGUE"]})
    assert resp.status_code == 409
    assert resp.json()["code"] == "REFLECT_ALREADY_TAGGED"


def test_tag_rejects_non_failed_execution(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _check_in(client, exec_id, "done")
    resp = client.post(f"/reflection/failure-tags/{exec_id}", json={"tagCodes": ["AMBIGUITY"]})
    assert resp.status_code == 422
    assert resp.json()["code"] == "REFLECT_NOT_FAILED"


# ───────────────────────── E2E: 중간발표 데모 루프 ─────────────────────────


def test_full_demo_loop_fail_to_recovery_action(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """start → 못함 → AMBIGUITY 태깅 → Recovery 카드(NANO_STEP) → 수락 → 새 5분 카드.

    중간발표 시연 시나리오 (Reaction_중간발표_데모시나리오_v1.0) 의 백엔드 전 구간.
    """
    # 1) 어제 계획했던 카드
    action = _seed_action(fake_action_item_repo, title="GROUP BY 실습")

    # 2) [▶ 시작] → 3) [못함] 체크인
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    check = _check_in(client, exec_id, "failed").json()
    assert check["needsFailureTags"] is True

    # 4) 실패 사유: 막막해서 시작 못 함
    tag = client.post(f"/reflection/failure-tags/{exec_id}", json={"tagCodes": ["AMBIGUITY"]})
    assert tag.status_code == 201

    # 5) Recovery 카드 생성 — AMBIGUITY → NANO_STEP 이 선두
    proposals = client.post("/recovery/proposals/generate", json={"executionId": exec_id}).json()
    assert proposals["isDraft"] is True
    top = proposals["cards"][0]
    assert top["strategyType"] == "NANO_STEP"
    assert "GROUP BY 실습" in top["suggestedActionText"]

    # 6) [수락] → 새 5분 카드 생성, 원본 status 는 failed 그대로
    decision = client.post(
        "/recovery/decisions",
        json={
            "executionId": exec_id,
            "decision": "accepted",
            "acceptedAttemptId": top["attemptId"],
        },
        headers={"Idempotency-Key": f"demo-{uuid4()}"},
    ).json()
    assert decision["resultingActionItemId"] is not None
    assert action.status == "failed"  # 원본 불변 — Resilience 지표 전제
    recovered = [
        a for a in fake_action_item_repo._items.values() if a.source == "recovery_downscope"
    ]
    assert len(recovered) == 1
    assert recovered[0].parent_action_item_id == action.id
