"""Loopback-only HTTP test adapter with in-memory repositories, never production.

Run: REENTRY_TEST_SERVER=1 uv run uvicorn tests.integration.reentry_server:app
 --host 127.0.0.1 --port 8019. Uses real routes and encryption, fake persistence/auth.
"""

import os
from uuid import UUID

if os.environ.get("REENTRY_TEST_SERVER") != "1":
    raise RuntimeError("Explicit REENTRY_TEST_SERVER=1 is required")
os.environ["APP_ENV"] = "local"
os.environ["JWT_SECRET"] = "local-reentry-contract-test-secret-not-for-production"
os.environ["COLUMN_ENCRYPTION_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["GEMINI_API_KEY"] = ""

from reaction_backend.api.deps import get_current_user
from reaction_backend.api.routes import work_context
from reaction_backend.config import get_settings
from reaction_backend.db.models.work_context import ActionResource, ExecutionNote
from reaction_backend.db.session import get_db
from reaction_backend.main import create_app
from reaction_backend.repositories.action_item_repo import get_action_item_repo
from reaction_backend.repositories.execution_repo import get_execution_repo
from reaction_backend.repositories.recovery_repo import get_recovery_repo
from reaction_backend.repositories.reentry_repo import get_reentry_repo
from tests.conftest import (
    FakeActionItemRepo,
    FakeExecutionRepo,
    FakeRecoveryRepo,
    _FakeSession,
    make_demo_user,
)
from tests.test_execution_checkin import _seed_action
from tests.test_reentry import MemoryReentryRepo

get_settings.cache_clear()
app = create_app()
actions = FakeActionItemRepo()
action = _seed_action(actions)
old_id = action.id
action.id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
actions._items.pop(old_id, None)
actions.seed(action)
action.first_step = "보고서 첫 문장 하나 쓰기"
executions = FakeExecutionRepo()
reentry = MemoryReentryRepo()


async def session():
    yield _FakeSession()


app.dependency_overrides[get_current_user] = make_demo_user
app.dependency_overrides[get_db] = session
app.dependency_overrides[get_action_item_repo] = lambda: actions
app.dependency_overrides[get_execution_repo] = lambda: executions
app.dependency_overrides[get_reentry_repo] = lambda: reentry
app.dependency_overrides[get_recovery_repo] = lambda: FakeRecoveryRepo()


# P1 HTTP integration: production route logic, isolated in-memory storage only.

resources = {}
notes = {}
test_user = make_demo_user()
test_user.focus_mode_preferences = {}


class ContextSession(_FakeSession):
    def add(self, row):
        if isinstance(row, ActionResource):
            resources[row.action_id] = row
        elif isinstance(row, ExecutionNote):
            notes[row.execution_id] = row


class MemoryContextRepo:
    def __init__(self, session):
        self.session = session

    async def lock_user(self, user_id):
        return test_user

    async def existing_action(self, action_id):
        return actions._items.get(action_id)

    async def add_action(self, action):
        actions.seed(action)

    async def goal(self, user_id, goal_id):
        return None

    async def resource(self, action_id):
        return resources.get(action_id)

    async def note(self, execution_id):
        return notes.get(execution_id)

    async def latest_note(self, user_id, action_id):
        return next(
            (
                n
                for eid, n in reversed(list(notes.items()))
                if executions._executions[eid].action_item_id == action_id
            ),
            None,
        )

    async def evidence(self, user_id, start, end):
        rows = [
            a
            for a in reentry.items.values()
            if a.entry_kind in ("restart", "resume") and start <= a.viewed_at < end
        ]
        completed = {
            a.execution_id
            for a in rows
            if a.execution_id in executions._executions
            and executions._executions[a.execution_id].completion_status in ("done", "over_done")
        }
        return (
            len(rows),
            sum(a.started_at is not None for a in rows),
            sum(a.step_completed_at is not None for a in rows),
            len(completed),
        )


async def context_session():
    yield ContextSession()


work_context.WorkContextRepo = MemoryContextRepo
app.dependency_overrides[get_current_user] = lambda: test_user
app.dependency_overrides[get_db] = context_session
