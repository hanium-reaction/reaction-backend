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
from reaction_backend.config import get_settings
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
