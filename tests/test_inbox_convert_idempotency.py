"""인박스 옮기기(convert-*)가 **한 메모 = 한 번** 인가 (inbox-2 / contract-3 / abuse-2 / data-4).

두 번 탭·재시도·보관 후 되살리기로 같은 메모에서 카드·목표가 하나씩 더 생겼다. 목표는
유지(Maintain ≤ 5) 한도까지 먹었다. 그리고 메모에 제한이 없는데 `goals.title` 은 200자라
긴 메모를 목표로 옮기면 500 이 났고 다시 눌러도 영영 안 됐다(inbox-3).

규칙:
- 같은 버튼을 다시 누르면 **이미 끝난 일** — 지금 상태를 200 으로(멱등, 새로 안 만든다).
- 다른 쪽으로 옮기려 하면 409 `INBOX_ALREADY_PROMOTED`(FE 가 문구를 이미 매핑해 둔 코드).
- 보관했다 되살려도 옮긴 항목은 `promoted` 로 돌아온다.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.inbox_item import InboxItem
from reaction_backend.db.models.user import User
from reaction_backend.safety.encryption import encrypt_inbox_text
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.errors import ApiError
from tests.conftest import DB_AVAILABLE


def _capture(client: TestClient, text_: str = "캡스톤 설계 단계 정리") -> str:
    resp = client.post("/inbox", json={"rawText": text_})
    assert resp.status_code == 201, resp.json()
    return str(resp.json()["inboxId"])


# ── 라우트 (fake repo) ─────────────────────────────────────────────────────


def test_converting_to_goal_twice_makes_one_goal(client: TestClient) -> None:
    inbox_id = _capture(client)

    first = client.post(f"/inbox/{inbox_id}/convert-to-goal")
    second = client.post(f"/inbox/{inbox_id}/convert-to-goal")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["promotedGoalId"] == first.json()["promotedGoalId"]
    assert len(client.get("/goals").json()["maintain"]) == 1


def test_converting_to_action_twice_makes_one_card(
    client: TestClient, fake_action_item_repo: Any
) -> None:
    inbox_id = _capture(client, "오늘 산책")

    assert client.post(f"/inbox/{inbox_id}/convert-to-action").status_code == 200
    again = client.post(f"/inbox/{inbox_id}/convert-to-action")

    assert again.status_code == 200
    assert again.json()["promotedTo"] == "action"
    assert len(fake_action_item_repo._items) == 1


def test_action_item_cannot_also_become_a_goal(client: TestClient) -> None:
    inbox_id = _capture(client, "오늘 산책")
    client.post(f"/inbox/{inbox_id}/convert-to-action")

    res = client.post(f"/inbox/{inbox_id}/convert-to-goal")

    assert res.status_code == 409
    assert res.json()["code"] == "INBOX_ALREADY_PROMOTED"
    assert client.get("/goals").json()["maintain"] == []


def test_goal_item_cannot_also_become_a_card(
    client: TestClient, fake_action_item_repo: Any
) -> None:
    inbox_id = _capture(client)
    client.post(f"/inbox/{inbox_id}/convert-to-goal")

    res = client.post(f"/inbox/{inbox_id}/convert-to-action")

    assert res.status_code == 409
    assert res.json()["code"] == "INBOX_ALREADY_PROMOTED"
    assert fake_action_item_repo._items == {}


def test_restoring_a_promoted_item_keeps_it_promoted(
    client: TestClient, fake_action_item_repo: Any
) -> None:
    """보관 → 되살리기가 옮기기 버튼을 다시 살려 카드가 또 생기던 경로."""
    inbox_id = _capture(client, "오늘 산책")
    client.post(f"/inbox/{inbox_id}/convert-to-action")
    client.post(f"/inbox/{inbox_id}/archive")

    restored = client.post(f"/inbox/{inbox_id}/restore")
    again = client.post(f"/inbox/{inbox_id}/convert-to-action")

    assert restored.status_code == 200
    assert restored.json()["status"] == "promoted"
    assert restored.json()["promotedTo"] == "action"
    assert again.status_code == 200
    assert len(fake_action_item_repo._items) == 1


def test_restoring_an_unpromoted_item_is_unchanged(client: TestClient) -> None:
    inbox_id = _capture(client)
    client.post(f"/inbox/{inbox_id}/archive")

    assert client.post(f"/inbox/{inbox_id}/restore").json()["status"] == "classified"


def test_patch_cannot_reopen_a_promoted_item(client: TestClient) -> None:
    inbox_id = _capture(client)
    client.post(f"/inbox/{inbox_id}/convert-to-goal")

    res = client.patch(f"/inbox/{inbox_id}", json={"status": "classified"})

    assert res.status_code == 409
    assert res.json()["code"] == "INBOX_ALREADY_PROMOTED"


def test_patch_to_archived_really_archives(client: TestClient) -> None:
    """status 만 바꾸고 `archived_at` 이 비어 활성 목록과 보관함에 동시에 뜨던 경로."""
    inbox_id = _capture(client)

    assert client.patch(f"/inbox/{inbox_id}", json={"status": "archived"}).status_code == 200

    assert client.get("/inbox").json() == []
    assert [i["inboxId"] for i in client.get("/inbox?status=archived").json()] == [inbox_id]


def test_long_memo_becomes_a_goal_with_a_clipped_title(client: TestClient) -> None:
    long_text = "가" * 600
    inbox_id = _capture(client, long_text)

    res = client.post(f"/inbox/{inbox_id}/convert-to-goal")

    assert res.status_code == 200
    title = client.get("/goals").json()["maintain"][0]["title"]
    assert len(title) == 200
    assert title.endswith("…")
    # 원문은 인박스 항목에 그대로 남는다
    assert res.json()["rawText"] == long_text


def test_unknown_status_filter_is_422_not_500(client: TestClient) -> None:
    res = client.get("/inbox?status=zzz")
    assert res.status_code == 422
    assert res.json()["code"] == "COMMON_VALIDATION_ERROR"


def test_convert_to_goal_limit_message_speaks_the_screen_language(client: TestClient) -> None:
    for i in range(5):
        client.post(
            "/goals",
            json={
                "title": f"m{i}",
                "category": "study",
                "goalTier": "maintain",
                "priorityLevel": 3,
            },
        )
    inbox_id = _capture(client, "over")

    res = client.post(f"/inbox/{inbox_id}/convert-to-goal")

    assert res.status_code == 422
    assert "유지 목표는 최대 5개" in res.json()["message"]
    assert "Maintain" not in res.json()["message"]


# ── 실 Postgres ────────────────────────────────────────────────────────────


async def test_locking_read_takes_a_row_lock() -> None:
    """`get_by_id_for_update` 는 FOR UPDATE + 미보관 조건을 같은 문장에 건다."""
    from reaction_backend.repositories.inbox_repo import InboxRepo

    class _Result:
        def scalar_one_or_none(self) -> None:
            return None

    class _Session:
        def __init__(self) -> None:
            self.statements: list[Any] = []

        async def execute(self, stmt: Any) -> _Result:
            self.statements.append(stmt)
            return _Result()

    session = _Session()
    await InboxRepo(session).get_by_id_for_update(uuid.uuid4(), uuid.uuid4())  # type: ignore[arg-type]

    sql = str(session.statements[0].compile())
    assert "FOR UPDATE" in sql
    assert "archived_at IS NULL" in sql


async def _seed_user(session: AsyncSession) -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@test.local", name="인박스 옮기기")
    session.add(user)
    await session.flush()
    return user


def _item(user: User, raw: str, *, source: str = "user", slug: str | None = None) -> InboxItem:
    return InboxItem(
        id=uuid.uuid4(),
        user_id=user.id,
        raw_text_encrypted=encrypt_inbox_text(raw),
        ai_category_guess="study",
        status="classified",
        source=source,
        resource_slug=slug,
    )


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")
async def test_restore_reads_the_card_made_from_the_item(real_db_session: AsyncSession) -> None:
    """할 일로 옮긴 증거는 그 항목에서 만든 카드다 — 추천 자료의 '한 걸음' 카드는 승격이 아니다."""
    from reaction_backend.repositories.inbox_repo import InboxRepo

    user = await _seed_user(real_db_session)
    promoted = _item(user, "오늘 산책")
    resource = _item(user, "자료", source="system", slug="exercise-plan-that-bends")
    plain = _item(user, "그냥 메모")
    real_db_session.add_all([promoted, resource, plain])
    await real_db_session.flush()
    for item in (promoted, resource):
        real_db_session.add(
            ActionItem(
                user_id=user.id,
                inbox_item_id=item.id,
                title="카드",
                target_date=now_kst().date(),
                category="study",
                status="planned",
                source="inbox",
            )
        )
    await real_db_session.flush()
    repo = InboxRepo(real_db_session)
    for item in (promoted, resource, plain):
        await repo.soft_delete(item)

    for item in (promoted, resource, plain):
        await repo.restore(item)

    assert promoted.status == "promoted"
    assert resource.status == "classified"
    assert plain.status == "classified"


async def _convert_via_route(session: AsyncSession, user: User, item: InboxItem) -> Any:
    from reaction_backend.api.routes import inbox as inbox_route
    from reaction_backend.repositories.goal_repo import GoalRepo
    from reaction_backend.repositories.inbox_repo import InboxRepo

    return await inbox_route.convert_to_goal(
        inbox_id=f"inbox_{item.id}",
        user=user,
        repo=InboxRepo(session),
        goal_repo=GoalRepo(session),
        session=session,
    )


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")
async def test_long_memo_to_goal_fits_the_real_title_column(
    real_db_session: AsyncSession,
) -> None:
    """fake repo 는 `String(200)` 을 모른다 — 실 컬럼에서 500 이 안 나는지 여기서 본다."""
    user = await _seed_user(real_db_session)
    item = _item(user, "가" * 250)
    real_db_session.add(item)
    await real_db_session.flush()
    real_db_session.commit = real_db_session.flush  # type: ignore[method-assign]

    body = await _convert_via_route(real_db_session, user, item)
    await real_db_session.flush()

    assert body.status == "promoted"
    titles = (
        (await real_db_session.execute(select(Goal.title).where(Goal.user_id == user.id)))
        .scalars()
        .all()
    )
    assert [len(t) for t in titles] == [200]


async def _sessionmaker() -> AsyncIterator[Any]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from reaction_backend.config import get_settings
    from reaction_backend.db.session import normalize_async_url

    engine = create_async_engine(
        normalize_async_url(get_settings().database_url), poolclass=NullPool
    )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")
async def test_concurrent_double_tap_makes_one_goal() -> None:
    """같은 메모를 두 요청이 동시에 옮겨도 목표는 하나다 — 실 커넥션 두 개."""
    agen = _sessionmaker()
    sm = await anext(agen)
    uid = uuid.uuid4()
    try:
        async with sm() as s:
            user = User(id=uid, email=f"inbox+{uid}@test.local", name="inbox double tap")
            s.add(user)
            await s.flush()
            item = _item(user, "러닝 30분")
            s.add(item)
            await s.commit()

        async def attempt() -> str:
            async with sm() as s:
                u = await s.get(User, uid)
                assert u is not None
                try:
                    body = await _convert_via_route(s, u, item)
                except ApiError as e:
                    return e.code
                return str(body.promoted_goal_id)

        results = await asyncio.wait_for(asyncio.gather(attempt(), attempt()), timeout=30)

        async with sm() as s:
            goals = (
                await s.execute(
                    select(func.count())
                    .select_from(Goal)
                    .where(Goal.user_id == uid, Goal.archived_at.is_(None))
                )
            ).scalar_one()
    finally:
        async with sm() as s:
            await s.execute(text("DELETE FROM inbox_items WHERE user_id = :u"), {"u": uid})
            await s.execute(text("DELETE FROM goals WHERE user_id = :u"), {"u": uid})
            await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            await s.commit()
        await agen.aclose()

    assert goals == 1
    assert results[0] == results[1]  # 둘 다 같은 목표를 가리킨다(두 번째는 멱등 200)
