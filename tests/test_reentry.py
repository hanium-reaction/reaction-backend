from datetime import timedelta
from uuid import uuid4

import pytest

from reaction_backend.repositories.reentry_repo import get_reentry_repo
from reaction_backend.schemas.common import now_kst
from tests.test_execution_checkin import _seed_action


class MemoryReentryRepo:
    def __init__(self):
        self.items = {}

    async def get(self, user_id, attempt_id):
        a = self.items.get(attempt_id)
        return a if a and a.user_id == user_id else None

    async def active(self, user_id, action_id):
        return next(
            (
                a
                for a in reversed(list(self.items.values()))
                if a.user_id == user_id and a.action_item_id == action_id and a.ended_at is None
            ),
            None,
        )

    async def add(self, attempt):
        self.items[attempt.id] = attempt


@pytest.fixture
def setup_reentry(client, fake_action_item_repo):
    repo = MemoryReentryRepo()
    client.app.dependency_overrides[get_reentry_repo] = lambda: repo
    action = _seed_action(fake_action_item_repo)
    action.first_step = "첫 문장 쓰기"
    return client, action, repo


def prepare(client, action, request_id=None):
    return client.post(
        f"/reentry/actions/action_{action.id}/prepare",
        json={"requestId": str(request_id or uuid4())},
    )


def start(client, aid, step="첫 문장 쓰기"):
    return client.post(f"/reentry/attempts/{aid}/start", json={"firstStep": step})


def event(client, aid, name):
    return client.post(f"/reentry/attempts/{aid}/events", json={"event": name})


def test_first_step_does_not_complete_task(setup_reentry, fake_execution_repo):
    client, action, repo = setup_reentry
    p = prepare(client, action).json()
    assert p["entryKind"] == "initial"
    assert action.status == "planned"
    assert len(fake_execution_repo._executions) == 0
    assert event(client, p["attemptId"], "first_step_completed").status_code == 409
    assert event(client, p["attemptId"], "viewed").status_code == 200
    s = start(client, p["attemptId"]).json()
    result = event(client, p["attemptId"], "first_step_completed")
    assert result.status_code == 200, result.text
    assert result.json()["stepCompletedAt"]
    assert result.json()["executionStatus"] == "in_progress"
    assert action.status == "in_progress"
    assert (
        event(client, p["attemptId"], "first_step_completed").json()["stepCompletedAt"]
        == result.json()["stepCompletedAt"]
    )
    assert start(client, p["attemptId"]).json()["executionId"] == s["executionId"]
    assert len(fake_execution_repo._executions) == 1
    assert "첫 문장" not in next(iter(repo.items.values())).first_step_encrypted


def test_leave_then_resume_reuses_attempt_and_execution(setup_reentry, fake_execution_repo):
    client, action, _ = setup_reentry
    p = prepare(client, action).json()
    s = start(client, p["attemptId"]).json()
    assert event(client, p["attemptId"], "leave").json()["executionStatus"] == "paused"
    restored = prepare(client, action).json()
    assert restored["attemptId"] == p["attemptId"]
    assert restored["executionStatus"] == "paused"
    again = start(client, p["attemptId"]).json()
    assert again["executionStatus"] == "in_progress"
    assert again["executionId"] == s["executionId"]
    assert len(fake_execution_repo._executions) == 1


def test_new_attempt_after_ended_execution_preserves_old(setup_reentry, fake_execution_repo):
    client, action, repo = setup_reentry
    p = prepare(client, action).json()
    s = start(client, p["attemptId"]).json()
    result = client.post(
        "/today/check-ins", json={"executionId": s["executionId"], "completionStatus": "failed"}
    )
    assert result.status_code == 200
    assert start(client, p["attemptId"]).status_code == 409
    again = prepare(client, action).json()
    assert again["entryKind"] == "restart"
    assert again["attemptId"] != p["attemptId"]
    assert start(client, again["attemptId"]).json()["executionId"] != s["executionId"]
    assert len(fake_execution_repo._executions) == 2
    assert list(repo.items.values())[0].ended_at


@pytest.mark.parametrize("source", ["recovery_downscope", "recovery_carryover"])
def test_unapproved_derived_action_is_blocked(setup_reentry, source):
    client, action, _ = setup_reentry
    action.source = source
    p = prepare(client, action).json()
    assert p["canStart"] is False
    assert start(client, p["attemptId"]).status_code == 409


def test_future_and_done_actions_block_start(setup_reentry):
    client, action, _ = setup_reentry
    action.target_date = now_kst().date() + timedelta(days=1)
    p = prepare(client, action).json()
    assert not p["canStart"]
    assert start(client, p["attemptId"]).status_code == 409
    action.target_date = now_kst().date()
    action.status = "done"
    assert start(client, p["attemptId"]).status_code == 409


def test_unknown_foreign_and_archived_are_not_found(setup_reentry):
    client, action, repo = setup_reentry
    assert start(client, uuid4()).status_code == 404
    p = prepare(client, action).json()
    next(iter(repo.items.values())).user_id = uuid4()
    assert start(client, p["attemptId"]).status_code == 404
    action.archived_at = now_kst()
    assert prepare(client, action).status_code == 404


def test_prepare_retries_do_not_inflate_exposures(setup_reentry):
    client, action, repo = setup_reentry
    key = uuid4()
    p = prepare(client, action, key).json()
    assert prepare(client, action, key).json()["attemptId"] == p["attemptId"]
    assert prepare(client, action).json()["attemptId"] == p["attemptId"]
    assert len(repo.items) == 1
    assert next(iter(repo.items.values())).viewed_at is None


def test_empty_step_is_not_invented(setup_reentry):
    client, action, _ = setup_reentry
    action.first_step = None
    p = prepare(client, action).json()
    assert p["firstStep"] is None
    assert start(client, p["attemptId"], " ").status_code == 409


def test_existing_paused_execution_is_server_authority(setup_reentry):
    client, action, _ = setup_reentry
    s = client.post(f"/today/actions/action_{action.id}/start").json()
    client.post(f"/today/focus/{s['executionId']}/pause")
    p = prepare(client, action).json()
    assert p["entryKind"] == "resume"
    assert p["executionStatus"] == "paused"
    assert start(client, p["attemptId"]).json()["executionId"] == s["executionId"]
