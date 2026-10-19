"""Real repositories and HTTP routes inside a rollback-only PostgreSQL transaction."""

from datetime import timedelta
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import get_current_user
from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.reentry_attempt import ReentryAttempt
from reaction_backend.db.models.user import User
from reaction_backend.db.session import get_db
from reaction_backend.main import create_app
from reaction_backend.repositories.privacy_repo import PrivacyRepo
from reaction_backend.schemas.common import now_kst


async def test_real_routes_persist_step_without_completion(real_db_session):
    db = real_db_session
    user = User(
        id=uuid4(), email=f"{uuid4()}@test.invalid", name="P0 test", onboarding_state="ACTIVE"
    )
    db.add(user)
    await db.flush()
    action = ActionItem(
        id=uuid4(),
        user_id=user.id,
        title="Test task",
        category="study",
        source="manual",
        status="planned",
        target_date=now_kst().date(),
        priority=3,
        estimated_minutes=15,
        first_step="Write one sentence",
    )
    db.add(action)
    await db.flush()
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user

    async def isolated_session():
        async with AsyncSession(
            bind=db.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as session:
            yield session

    app.dependency_overrides[get_db] = isolated_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        p = await client.post(
            f"/reentry/actions/action_{action.id}/prepare", json={"requestId": str(uuid4())}
        )
        assert p.status_code == 200, p.text
        aid = p.json()["attemptId"]
        await client.post(f"/reentry/attempts/{aid}/events", json={"event": "viewed"})
        s = await client.post(
            f"/reentry/attempts/{aid}/start", json={"firstStep": "Write one sentence"}
        )
        assert s.status_code == 200, s.text
        retry = await client.post(
            f"/reentry/attempts/{aid}/start", json={"firstStep": "ignored after start"}
        )
        assert retry.json()["executionId"] == s.json()["executionId"]
        report = await client.post(
            f"/reentry/attempts/{aid}/events", json={"event": "first_step_completed"}
        )
        assert report.status_code == 200, report.text
        assert report.json()["stepCompletedAt"]
        await db.refresh(action)
        assert action.status == "in_progress"
        from uuid import UUID

        execution = await db.get(
            ExecutionEvent, UUID(s.json()["executionId"].removeprefix("exec_"))
        )
        assert execution.completion_status == "in_progress"
        assert execution.actual_end_at is None
        record = await db.get(ReentryAttempt, UUID(aid))
        assert record.started_at and record.viewed_at and record.step_completed_at
        # Pause persistence and server-authoritative restoration, not browser-only state.
        left = await client.post(f"/reentry/attempts/{aid}/events", json={"event": "leave"})
        assert left.json()["executionStatus"] == "paused"
        again = await client.post(
            f"/reentry/actions/action_{action.id}/prepare", json={"requestId": str(uuid4())}
        )
        assert again.json()["attemptId"] == aid
        assert again.json()["executionStatus"] == "paused"
        await PrivacyRepo(db).anonymize_user(user.id)
        await db.refresh(record)
        assert record.first_step_encrypted is None


async def test_real_repo_approval_guard(real_db_session):
    from reaction_backend.api.routes.reentry import blocked
    from reaction_backend.repositories.execution_repo import ExecutionRepo

    db = real_db_session
    user = User(
        id=uuid4(), email=f"{uuid4()}@test.invalid", name="P0 test", onboarding_state="ACTIVE"
    )
    db.add(user)
    await db.flush()
    action = ActionItem(
        id=uuid4(),
        user_id=user.id,
        title="Draft recovery",
        category="study",
        source="recovery_downscope",
        status="planned",
        target_date=now_kst().date(),
        priority=3,
        estimated_minutes=10,
    )
    db.add(action)
    await db.flush()
    assert await blocked(action, ExecutionRepo(db)) is not None
    block = await ExecutionRepo(db).create_adhoc_block(
        user_id=user.id, action_item=action, start_at=now_kst() - timedelta(minutes=1)
    )
    block.source = "recovery"
    await db.flush()
    assert await blocked(action, ExecutionRepo(db)) is None
