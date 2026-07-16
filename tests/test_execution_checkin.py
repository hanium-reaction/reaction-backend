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


# ───────────────────────── focus pause / resume (#83) ─────────────────────────


def _pause(client: TestClient, execution_id: str) -> Any:
    return client.post(f"/today/focus/{execution_id}/pause")


def _resume(client: TestClient, execution_id: str) -> Any:
    return client.post(f"/today/focus/{execution_id}/resume")


def test_pause_then_resume(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """[⏸]→[▶] 정지/재개: execution 은 in_progress 유지, interruption 구간이 닫힌다."""
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]

    p = _pause(client, exec_id)
    assert p.status_code == 200
    body = p.json()
    assert body["status"] == "paused"
    assert body["executionId"] == exec_id
    assert body["actionItemId"] == f"action_{action.id}"
    assert body["pauseTotalMinutes"] == 0

    r = _resume(client, exec_id)
    assert r.status_code == 200
    assert r.json()["status"] == "in_progress"

    # 정지 구간이 닫혔는지 (재개 표시 + 지연분 기록)
    interruptions = list(fake_execution_repo._interruptions.values())
    assert len(interruptions) == 1
    assert interruptions[0].resumed_after_interrupt is True
    assert interruptions[0].resume_delay_minutes is not None
    # execution 은 여전히 진행 중 (체크인 전)
    execution = next(iter(fake_execution_repo._executions.values()))
    assert execution.completion_status == "in_progress"


def test_resume_accumulates_pause_minutes(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """재개 시 정지 시작(created_at)부터의 경과가 pause_total_minutes 로 누적된다."""
    from datetime import timedelta

    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _pause(client, exec_id)
    # 정지 시작을 10분 전으로 되돌려 누적 검증
    pause = next(iter(fake_execution_repo._interruptions.values()))
    pause.created_at = pause.created_at - timedelta(minutes=10)

    body = _resume(client, exec_id).json()
    assert body["pauseTotalMinutes"] == 10
    execution = next(iter(fake_execution_repo._executions.values()))
    assert execution.pause_total_minutes == 10


def test_pause_conflict_when_already_paused(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _pause(client, exec_id)
    resp = _pause(client, exec_id)
    assert resp.status_code == 409
    assert resp.json()["code"] == "TODAY_ALREADY_PAUSED"


def test_resume_conflict_when_not_paused(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    resp = _resume(client, exec_id)
    assert resp.status_code == 409
    assert resp.json()["code"] == "TODAY_NOT_PAUSED"


def test_pause_conflict_after_check_in(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """체크인으로 종결된 실행은 정지할 수 없다 (409)."""
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _check_in(client, exec_id, "done")
    resp = _pause(client, exec_id)
    assert resp.status_code == 409
    assert resp.json()["code"] == "TODAY_ALREADY_CHECKED_IN"


def test_pause_404_unknown_execution(client: TestClient) -> None:
    resp = _pause(client, f"exec_{uuid4()}")
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


# ───────────────────────── reflection/pending (#83) ─────────────────────────


def test_reflection_pending_lists_unchecked_execution(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """시작만 하고 체크인 안 한 실행이 저녁 회고 pending 에 뜬다 (completionStatus null)."""
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]

    resp = client.get("/reflection/pending")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    item = items[0]
    assert item["executionId"] == exec_id
    assert item["actionItemId"] == f"action_{action.id}"
    assert item["title"] == action.title
    assert item["completionStatus"] is None
    assert item["scheduledTime"] is not None  # "HH:MM"


def test_reflection_pending_excludes_checked_in(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """체크인으로 종결된 실행은 pending 에서 빠진다."""
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _check_in(client, exec_id, "done")
    assert client.get("/reflection/pending").json() == []


def test_reflection_pending_empty_when_nothing_started(client: TestClient) -> None:
    assert client.get("/reflection/pending").json() == []


# ───────────────────────── reflection/batch (§11) ─────────────────────────


def _batch(client: TestClient, items: list[dict[str, Any]]) -> Any:
    # 매 호출 고유 Idempotency-Key — 미들웨어 캐시 교차오염 방지.
    return client.post(
        "/reflection/batch",
        json={"items": items},
        headers={"Idempotency-Key": f"batch-{uuid4()}"},
    )


def test_reflection_batch_checks_in_all(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """미체크 실행 2건 일괄 종결 — done + failed(사유 포함). pending 에서 빠지고 status 전이."""
    a1 = _seed_action(fake_action_item_repo, title="A1")
    a2 = _seed_action(fake_action_item_repo, title="A2")
    e1 = _start(client, f"action_{a1.id}").json()["executionId"]
    e2 = _start(client, f"action_{a2.id}").json()["executionId"]

    resp = _batch(
        client,
        [
            {"executionId": e1, "completionStatus": "done"},
            {
                "executionId": e2,
                "completionStatus": "failed",
                "failureTags": ["AMBIGUITY"],
                "memo": "막막함",
            },
        ],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["processedCount"] == 2
    assert body["taggedCount"] == 1
    assert body["needsFailureTags"] == []
    assert client.get("/reflection/pending").json() == []
    assert a1.status == "done"
    assert a2.status == "failed"


def test_reflection_batch_flags_untagged_failures(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """failed 인데 사유 미기록 → needsFailureTags 에 executionId (FE 가 S18 로 유도)."""
    a = _seed_action(fake_action_item_repo)
    e = _start(client, f"action_{a.id}").json()["executionId"]
    body = _batch(client, [{"executionId": e, "completionStatus": "failed"}]).json()
    assert body["processedCount"] == 1
    assert body["taggedCount"] == 0
    assert body["needsFailureTags"] == [e]


def test_reflection_batch_empty_is_noop(client: TestClient) -> None:
    resp = _batch(client, [])
    assert resp.status_code == 200
    assert resp.json()["processedCount"] == 0


def test_reflection_batch_requires_idempotency_key(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    a = _seed_action(fake_action_item_repo)
    e = _start(client, f"action_{a.id}").json()["executionId"]
    resp = client.post(
        "/reflection/batch",
        json={"items": [{"executionId": e, "completionStatus": "done"}]},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_reflection_batch_rejects_tags_on_non_failure(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    a = _seed_action(fake_action_item_repo)
    e = _start(client, f"action_{a.id}").json()["executionId"]
    resp = _batch(
        client, [{"executionId": e, "completionStatus": "done", "failureTags": ["AMBIGUITY"]}]
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "REFLECT_NOT_FAILED"


def test_reflection_batch_atomic_rollback_on_invalid_item(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """한 항목이 이미 체크인됐으면 전체 롤백 — 유효 항목도 미적용(여전히 pending)."""
    a1 = _seed_action(fake_action_item_repo, title="A1")
    a2 = _seed_action(fake_action_item_repo, title="A2")
    e1 = _start(client, f"action_{a1.id}").json()["executionId"]
    e2 = _start(client, f"action_{a2.id}").json()["executionId"]
    _check_in(client, e2, "done")  # e2 미리 종결

    resp = _batch(
        client,
        [
            {"executionId": e1, "completionStatus": "done"},
            {"executionId": e2, "completionStatus": "done"},  # 이미 체크인 → 409
        ],
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "TODAY_ALREADY_CHECKED_IN"
    # e1 은 검증 단계에서 막혀 미적용 — 아직 pending 에 남아있다.
    pending_ids = [i["executionId"] for i in client.get("/reflection/pending").json()]
    assert e1 in pending_ids
    assert a1.status != "done"


def test_reflection_batch_rejects_duplicate_execution(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    a = _seed_action(fake_action_item_repo)
    e = _start(client, f"action_{a.id}").json()["executionId"]
    resp = _batch(
        client,
        [
            {"executionId": e, "completionStatus": "done"},
            {"executionId": e, "completionStatus": "failed"},
        ],
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_reflection_batch_does_not_resurrect_cancelled_block(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """만료 cron(#20)이 취소한 블록을 stale 한 batch 요청이 finished 로 되살리지 않는다.

    회고 화면을 켜 둔 채 04:00 을 넘기면 cron 이 카드를 보관하고 블록을 cancel 한다. 그 뒤
    도착한 [모두 완료] 가 블록을 finished 로 덮으면, list_week(archived 를 안 보고 cancelled 만
    제외)에 유령 블록이 되살아난다.
    """
    action = _seed_action(fake_action_item_repo, title="만료 예정 카드")
    execution_id = _start(client, f"action_{action.id}").json()["executionId"]
    block = next(iter(fake_execution_repo._blocks.values()))
    block.block_status = "cancelled"  # cron 이 만료시킨 상태

    resp = _batch(client, [{"executionId": execution_id, "completionStatus": "done"}])

    assert resp.status_code == 200, resp.text
    assert block.block_status == "cancelled"
