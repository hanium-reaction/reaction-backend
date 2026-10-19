from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import get_current_user
from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.user import User
from reaction_backend.db.models.work_context import ActionResource, ExecutionNote
from reaction_backend.db.session import get_db
from reaction_backend.main import create_app
from reaction_backend.repositories.privacy_repo import PrivacyRepo
from reaction_backend.schemas.common import now_kst


@pytest.fixture
async def context_client(real_db_session):
    db = real_db_session
    user = User(
        id=uuid4(), email=f"{uuid4()}@test.invalid", name="Context test", onboarding_state="ACTIVE"
    )
    db.add(user)
    await db.flush()
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user

    async def isolated():
        async with AsyncSession(
            bind=db.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as session:
            yield session

    app.dependency_overrides[get_db] = isolated
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, db, user


async def quick(client):
    body = {
        "requestId": str(uuid4()),
        "title": "보고서",
        "firstStep": "첫 문장",
        "attemptMinutes": 10,
    }
    r = await client.post("/work-context/quick-action", json=body)
    assert r.status_code == 200, r.text
    return r.json(), body


async def test_quick_resource_execution_note_full_flow(context_client):
    c, db, user = context_client
    a, body = await quick(c)
    again = await c.post("/work-context/quick-action", json=body)
    assert again.json()["actionId"] == a["actionId"]
    changed = await c.post("/work-context/quick-action", json={**body, "title": "다른 일"})
    assert changed.status_code == 409
    assert user.onboarding_state == "ACTIVE"
    path = f"/work-context/actions/{a['actionId']}/resource"
    link = {"url": "https://example.org/private-notes", "label": "보고서 자료"}
    assert (await c.put(path, json=link)).status_code == 200
    assert (await c.get(path)).json()["resource"] == link
    record = await db.get(ActionResource, UUID(a["actionId"].removeprefix("action_")))
    assert "private-notes" not in record.content_encrypted
    p = await c.post(f"/reentry/actions/{a['actionId']}/prepare", json={"requestId": str(uuid4())})
    assert p.json()["estimatedMinutes"] == 10
    attempt = p.json()["attemptId"]
    started = await c.post(f"/reentry/attempts/{attempt}/start", json={"firstStep": "첫 문장"})
    eid = started.json()["executionId"]
    note_path = f"/work-context/executions/{eid}/note"
    for _ in range(2):
        assert (await c.put(note_path, json={"memo": "다음 문장부터"})).status_code == 200
    assert (await c.get(note_path)).json()["memo"] == "다음 문장부터"
    note = await db.get(ExecutionNote, UUID(eid.removeprefix("exec_")))
    assert "다음 문장" not in note.memo_encrypted
    assert (await c.get(f"/work-context/actions/{a['actionId']}/last-note")).json()[
        "memo"
    ] == "다음 문장부터"
    assert (await c.post(path + "/unlink")).json() == {"resource": None}
    await PrivacyRepo(db).anonymize_user(user.id)
    await db.refresh(note)
    assert note.memo_encrypted is None


async def test_resource_ownership_archive_and_preference(context_client):
    c, db, user = context_client
    a, body = await quick(c)
    assert (
        await c.put(
            f"/work-context/actions/action_{uuid4()}/resource",
            json={"url": "https://example.org", "label": "자료"},
        )
    ).status_code == 404
    assert (
        await c.put("/work-context/preference", json={"preference": "context_first"})
    ).status_code == 200
    await db.refresh(user)
    assert (await c.get("/work-context/preference")).json()["preference"] == "context_first"
    evidence = await c.get(
        "/work-context/evidence", params={"week_start": now_kst().date().isoformat()}
    )
    assert evidence.json()["attempts"] == 0  # initial work is not recovery evidence
    assert (await c.put("/work-context/preference", json={"preference": None})).status_code == 200
    await db.refresh(user)
    assert "reentry_preference" not in user.focus_mode_preferences
    action = await db.get(ActionItem, UUID(a["actionId"].removeprefix("action_")))
    action.archived_at = now_kst()
    await db.flush()
    assert (await c.post("/work-context/quick-action", json=body)).status_code == 404


async def test_other_user_cannot_read_or_modify_context(context_client):
    c, db, user = context_client
    other = User(
        id=uuid4(), email=f"{uuid4()}@test.invalid", name="Other", onboarding_state="ACTIVE"
    )
    db.add(other)
    await db.flush()
    item = ActionItem(
        id=uuid4(),
        user_id=other.id,
        title="private",
        first_step="secret",
        estimated_minutes=10,
        target_date=now_kst().date(),
        source="manual",
        status="planned",
        priority=3,
        category="other",
    )
    db.add(item)
    await db.flush()
    path = f"/work-context/actions/action_{item.id}"
    assert (await c.get(path + "/resource")).status_code == 404
    assert (await c.get(path + "/last-note")).status_code == 404
    assert (
        await c.put(path + "/resource", json={"url": "https://example.org", "label": "no"})
    ).status_code == 404
    assert (await c.post(path + "/resource/unlink")).status_code == 404


async def test_evidence_window_and_deduplicated_completion(context_client):
    from datetime import datetime, time, timedelta

    from reaction_backend.db.models.execution_event import ExecutionEvent
    from reaction_backend.db.models.reentry_attempt import ReentryAttempt
    from reaction_backend.repositories.work_context_repo import WorkContextRepo
    from reaction_backend.schemas.common import KST

    c, db, user = context_client
    a, _ = await quick(c)
    prepared = (
        await c.post(f"/reentry/actions/{a['actionId']}/prepare", json={"requestId": str(uuid4())})
    ).json()
    result = await c.post(
        f"/reentry/attempts/{prepared['attemptId']}/start", json={"firstStep": "첫 문장"}
    )
    assert result.status_code == 200, result.text
    eid = UUID(result.json()["executionId"].removeprefix("exec_"))
    execution = await db.get(ExecutionEvent, eid)
    start = datetime.combine(now_kst().date(), time.min, tzinfo=KST)
    end = start + timedelta(days=7)
    execution.completion_status = "done"
    execution.actual_end_at = start + timedelta(hours=1)
    for offset, kind in [
        (0, "restart"),
        (1, "resume"),
        (7, "restart"),
        (-1, "restart"),
        (0, "initial"),
    ]:
        stamp = start + timedelta(days=offset)
        db.add(
            ReentryAttempt(
                id=uuid4(),
                user_id=user.id,
                action_item_id=execution.action_item_id,
                execution_id=eid,
                entry_kind=kind,
                viewed_at=stamp,
                started_at=stamp,
                step_completed_at=stamp,
                ended_at=stamp,
            )
        )
    await db.flush()
    repo = WorkContextRepo(db)
    assert await repo.evidence(user.id, start, end) == (2, 2, 2, 1)
    execution.actual_end_at = end
    await db.flush()
    assert await repo.evidence(user.id, start, end) == (2, 2, 2, 0)
