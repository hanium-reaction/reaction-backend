"""Today 실행 쓰기 (#19-B) — start / check-in / failure-tags (api-contract §10·§11).

마지막 E2E 테스트는 중간발표 데모 루프 그 자체:
start → 못함 체크인 → 실패 사유(AMBIGUITY) → Recovery 카드(NANO_STEP) → 수락 → 새 카드.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID, uuid4

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


def test_start_again_returns_the_running_execution(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """앱이 죽어 sessionStorage 가 비면 FE 는 [이어서 하기] 에서 start 를 다시 부른다 (today-1).

    예전엔 409 `TODAY_EXECUTION_ALREADY_ACTIVE` 가 끝없이 반복돼 [완료] 가 영영 막혔다.
    이제 같은 실행을 200 으로 돌려주고, 아무것도 새로 만들지 않는다.
    """
    action = _seed_action(fake_action_item_repo)
    first = _start(client, f"action_{action.id}")
    assert first.status_code == 201
    again = _start(client, f"action_{action.id}")
    assert again.status_code == 200, again.json()
    body = again.json()
    assert body["executionId"] == first.json()["executionId"]
    assert body["actionId"] == f"action_{action.id}"
    assert body["completionStatus"] == "in_progress"
    # 타이머를 이어 붙일 기준 — 두 번째 호출 시각이 아니라 처음 시작한 시각
    assert body["actualStartAt"] == first.json()["actualStartAt"]
    assert len(fake_execution_repo._executions) == 1
    assert len(fake_execution_repo._blocks) == 1

    # 돌려받은 id 로 완료할 수 있다 — 막혀 있던 바로 그 경로
    done = _check_in(client, body["executionId"], "done")
    assert done.status_code == 200, done.json()
    assert action.status == "done"


def test_start_after_check_in_creates_a_new_execution(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """멱등은 **진행 중일 때만** — 끝난 실행을 되살려 돌려주지 않는다."""
    action = _seed_action(fake_action_item_repo)
    first = _start(client, f"action_{action.id}").json()["executionId"]
    _check_in(client, first, "partial_done")
    again = _start(client, f"action_{action.id}")
    assert again.status_code == 201
    assert again.json()["executionId"] != first
    assert len(fake_execution_repo._executions) == 2


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


def test_pause_twice_is_idempotent(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """정지 응답을 잃은 FE 의 재전송 — 409 대신 200 `paused`, 정지 구간은 하나 (today-5)."""
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    assert _pause(client, exec_id).status_code == 200

    resp = _pause(client, exec_id)

    assert resp.status_code == 200, resp.json()
    assert resp.json()["status"] == "paused"
    assert len(fake_execution_repo._interruptions) == 1


def test_resume_when_not_paused_is_a_no_op(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """재개 응답을 잃은 FE 의 재전송 — 409 대신 200 `in_progress`, 아무것도 안 바꾼다 (today-5)."""
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _pause(client, exec_id)
    assert _resume(client, exec_id).status_code == 200
    pause = next(iter(fake_execution_repo._interruptions.values()))
    settled = (pause.resume_delay_minutes, pause.resumed_after_interrupt)

    resp = _resume(client, exec_id)

    assert resp.status_code == 200, resp.json()
    assert resp.json()["status"] == "in_progress"
    assert (pause.resume_delay_minutes, pause.resumed_after_interrupt) == settled
    # 한 번도 정지 안 한 실행도 같다
    other = _seed_action(fake_action_item_repo, title="다른 카드")
    other_exec = _start(client, f"action_{other.id}").json()["executionId"]
    assert _resume(client, other_exec).json()["status"] == "in_progress"


def test_resume_after_the_6h_resolver_still_resumes(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """아침에 멈추고 저녁에 [계속] — 6h cron 이 마감 표시한 정지도 재개된다 (sched-14).

    예전엔 409 `TODAY_NOT_PAUSED` 가 영영 반복됐고, 멈춘 시간은 정지 시간에 안 들어갔다.
    cron 의 '6시간 안에 안 돌아옴'(False) 표시는 그대로 둔다.
    """
    import asyncio
    from datetime import timedelta

    from reaction_backend.repositories.interruption_event_repo import InterruptionEventRepo
    from reaction_backend.scheduler.interruption_resolver import run_interruption_resolver
    from reaction_backend.schemas.common import now_kst

    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _pause(client, exec_id)
    pause = next(iter(fake_execution_repo._interruptions.values()))
    pause.created_at = pause.created_at - timedelta(hours=10)

    class _Session:  # 실 repo 의 mark_unresumed 는 flush 만 부른다
        async def flush(self) -> None:
            return None

    repo = InterruptionEventRepo(_Session())  # type: ignore[arg-type]

    async def _stale(*, before: Any) -> list[Any]:
        return [pause] if pause.created_at < before else []

    repo.list_stale_unresolved = _stale  # type: ignore[method-assign]
    assert asyncio.run(run_interruption_resolver(now_kst(), repo=repo)) == 1
    assert pause.resumed_after_interrupt is False

    # 앱이 '정지 중' 이라고 믿고 다시 보낸 pause 도 새 구간을 열지 않는다.
    assert _pause(client, exec_id).json()["status"] == "paused"
    assert len(fake_execution_repo._interruptions) == 1

    resp = _resume(client, exec_id)

    assert resp.status_code == 200, resp.json()
    assert resp.json()["status"] == "in_progress"
    assert resp.json()["pauseTotalMinutes"] == 600
    assert pause.resume_delay_minutes == 600
    assert pause.resumed_after_interrupt is False  # cron 의 사실은 덮지 않는다
    assert _check_in(client, exec_id, "done").status_code == 200


def _backdate_start(fake_execution_repo: FakeExecutionRepo, minutes: int) -> Any:
    from datetime import timedelta

    execution = next(iter(fake_execution_repo._executions.values()))
    execution.actual_start_at = execution.actual_start_at - timedelta(minutes=minutes)
    return execution


def _backdate_pause(fake_execution_repo: FakeExecutionRepo, minutes: int) -> Any:
    from datetime import timedelta

    pause = next(iter(fake_execution_repo._interruptions.values()))
    pause.created_at = pause.created_at - timedelta(minutes=minutes)
    return pause


def test_actual_duration_excludes_paused_time(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """60분 전에 시작해 30분 멈췄다 재개하고 완료 — 실제 소요는 30분 (today-11)."""
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _backdate_start(fake_execution_repo, 60)
    _pause(client, exec_id)
    _backdate_pause(fake_execution_repo, 30)
    assert _resume(client, exec_id).json()["pauseTotalMinutes"] == 30

    body = _check_in(client, exec_id, "done").json()

    assert body["actualDurationMinutes"] == 30


def test_check_in_while_paused_closes_the_pause(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """정지 중에 바로 완료 — 그 정지를 닫고(재개 없이 끝냄) 정지 시간을 소요에서 뺀다 (today-11)."""
    import asyncio

    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    execution = _backdate_start(fake_execution_repo, 60)
    _pause(client, exec_id)
    pause = _backdate_pause(fake_execution_repo, 20)

    body = _check_in(client, exec_id, "done").json()

    assert body["actualDurationMinutes"] == 40
    assert pause.resume_delay_minutes == 20
    assert pause.resumed_after_interrupt is False
    assert execution.pause_total_minutes == 20
    assert asyncio.run(fake_execution_repo.get_open_pause(execution.id)) is None


def test_evening_reflection_leaves_actual_duration_unknown(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """저녁 회고는 소급 종결 — 회고한 시각은 끝낸 시각이 아니라 소요 시간을 지어내지 않는다.

    예전엔 13:00 에 시작한 30분짜리 카드를 21:30 에 '완료' 로 회고하면 510분이 기록됐다.
    """
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    execution = _backdate_start(fake_execution_repo, 510)

    resp = _batch(client, [{"executionId": exec_id, "completionStatus": "done"}])

    assert resp.status_code == 200, resp.json()
    assert execution.completion_status == "done"
    assert execution.actual_end_at is not None
    assert execution.actual_duration_minutes is None


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


def test_tag_failure_reasons_memo_without_tags_rejected(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """태그 0개 + memo — memo 는 태그 행에 얹혀 저장되므로 얹을 곳이 없다(#203).

    이전엔 여기서 200 이 나가고 memo 가 조용히 버려지면서 응답만 hasMemo=true 로 거짓
    보고했다. 이제는 저장하지 못할 걸 알고 422 로 막는다.
    """
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _check_in(client, exec_id, "failed")
    resp = client.post(
        f"/reflection/failure-tags/{exec_id}",
        json={"tagCodes": [], "memo": "말할 게 있는데 태그로는 안 골라져요"},
    )
    assert resp.status_code == 422, resp.json()
    assert resp.json()["field"] == "memo"


def test_tag_failure_reasons_stores_task_aversiveness(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """정서 1문항(#299, FE #222) — 실행 행에 그대로 저장된다."""
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _check_in(client, exec_id, "failed")
    resp = client.post(
        f"/reflection/failure-tags/{exec_id}",
        json={"tagCodes": ["AMBIGUITY"], "taskAversiveness": 5},
    )
    assert resp.status_code == 201, resp.json()
    stored = fake_execution_repo._executions[UUID(exec_id.removeprefix("exec_"))]
    assert stored.task_aversiveness == 5


def test_tag_failure_reasons_task_aversiveness_out_of_range(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _check_in(client, exec_id, "failed")
    resp = client.post(
        f"/reflection/failure-tags/{exec_id}",
        json={"tagCodes": ["AMBIGUITY"], "taskAversiveness": 6},
    )
    assert resp.status_code == 422


def test_tag_failure_reasons_task_aversiveness_omitted_leaves_null(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """선택 사항 — 안 보내면 NULL 로 남는다(강제 아님)."""
    action = _seed_action(fake_action_item_repo)
    exec_id = _start(client, f"action_{action.id}").json()["executionId"]
    _check_in(client, exec_id, "failed")
    resp = client.post(f"/reflection/failure-tags/{exec_id}", json={"tagCodes": ["AMBIGUITY"]})
    assert resp.status_code == 201, resp.json()
    stored = fake_execution_repo._executions[UUID(exec_id.removeprefix("exec_"))]
    assert stored.task_aversiveness is None


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


def test_reflection_batch_rejects_memo_on_non_failure(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """done + memo — 스키마 docstring 이 항상 주장해온 422 인데 코드가 실제론 안 막고

    있었다(#203). 그 결과 done 항목의 memo 가 200 으로 통과된 뒤 조용히 버려졌다.
    """
    a = _seed_action(fake_action_item_repo)
    e = _start(client, f"action_{a.id}").json()["executionId"]
    resp = _batch(client, [{"executionId": e, "completionStatus": "done", "memo": "잘 됐다"}])
    assert resp.status_code == 422
    assert resp.json()["code"] == "REFLECT_NOT_FAILED"
    assert resp.json()["field"] == "memo"


def test_reflection_batch_rejects_memo_without_tags(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """failed + memo + 태그 0개 — memo 는 태그 행에 얹혀 저장되므로 얹을 곳이 없다(#203).

    이전엔 이 조합이 200 으로 통과하며 memo 만 조용히 버려지고 needsFailureTags 로만
    빠졌다. 이제는 저장 못 할 걸 알고 전체 배치를 422 로 막는다(부분 적용 없음).
    """
    a = _seed_action(fake_action_item_repo)
    e = _start(client, f"action_{a.id}").json()["executionId"]
    resp = _batch(
        client, [{"executionId": e, "completionStatus": "failed", "memo": "태그로는 안 골라져요"}]
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "COMMON_VALIDATION_ERROR"
    assert resp.json()["field"] == "memo"


def test_reflection_batch_stores_task_aversiveness(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    a = _seed_action(fake_action_item_repo)
    e = _start(client, f"action_{a.id}").json()["executionId"]
    resp = _batch(
        client,
        [{"executionId": e, "completionStatus": "failed", "taskAversiveness": 4}],
    )
    assert resp.status_code == 200, resp.text
    stored = fake_execution_repo._executions[UUID(e.removeprefix("exec_"))]
    assert stored.task_aversiveness == 4


def test_reflection_batch_rejects_task_aversiveness_on_non_failure(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
) -> None:
    """codes 없이 정서 문항만 왔어도 completionStatus 가 실패군이 아니면 422."""
    a = _seed_action(fake_action_item_repo)
    e = _start(client, f"action_{a.id}").json()["executionId"]
    resp = _batch(client, [{"executionId": e, "completionStatus": "done", "taskAversiveness": 3}])
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


def test_check_in_does_not_resurrect_cancelled_block(
    client: TestClient,
    fake_action_item_repo: FakeActionItemRepo,
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """만료 cron(#20)이 취소한 블록을 stale 한 체크인이 finished 로 되살리지 않는다.

    `test_reflection_batch_does_not_resurrect_cancelled_block` 의 대칭 — batch 와 체크인은
    같은 쓰기(`block.block_status = "finished"`)를 하는데, 가드가 batch 에만 있었다.
    Focus 화면을 켜 둔 채 04:00 을 넘기면 cron 이 카드를 보관하고 블록을 cancel 하는데,
    그 뒤 도착한 체크인이 블록을 되살리면 list_week(archived 를 안 보고 cancelled 만 제외)에
    유령 블록이 뜬다 — 카드는 사라졌는데 주간 그리드엔 남는다.
    """
    action = _seed_action(fake_action_item_repo, title="만료 예정 카드")
    execution_id = _start(client, f"action_{action.id}").json()["executionId"]
    block = next(iter(fake_execution_repo._blocks.values()))
    block.block_status = "cancelled"  # cron 이 만료시킨 상태

    resp = _check_in(client, execution_id, "done")

    assert resp.status_code == 200, resp.text
    assert block.block_status == "cancelled"
