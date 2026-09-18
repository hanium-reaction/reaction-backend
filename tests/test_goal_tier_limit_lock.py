"""Focus ≤ 3 / Maintain ≤ 5 가 **동시 요청에서도** 지켜지는가 (goals-4 / abuse-1 / goals-9).

잠금 결정(DevBaseline §1.4)인데 "세고 → 넣기" 사이에 잠금이 없었다. 느린 모바일에서 '추가'
를 두 번 누르면 두 요청이 같은 개수를 읽고 둘 다 통과해 Focus 가 4개가 됐다(미러 재현:
동시 5건 → focus 5개). 한 번 넘으면 되돌릴 방법이 없다.

세 층으로 고정한다:
1. **순서 핀** — lock 을 잡은 **뒤에** 센다(반대면 lock 이 있어도 소용없다).
2. **실 동시성** — 커넥션 두 개로 실제 교차를 만들어 3개에서 멈추는지.
3. **배선·문구** — 라우트가 이 경로를 쓰고, 422 문구가 화면 말(집중/유지)인지.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.user import User
from reaction_backend.orchestrator import goal_policy
from reaction_backend.schemas.errors import ApiError
from tests.conftest import DB_AVAILABLE, DEMO_USER_UUID, FakeGoalRepo

# ── ① 순서 핀 — DB 없이 호출 순서만 본다 ─────────────────────────────────


class _Log:
    def __init__(self) -> None:
        self.events: list[str] = []


class _RecordingSession:
    def __init__(self, log: _Log) -> None:
        self._log = log

    async def execute(self, stmt: Any, params: Any = None) -> None:  # noqa: ARG002
        self._log.events.append(str(stmt))

    async def scalar(self, stmt: Any, params: Any = None) -> bool:  # noqa: ARG002
        self._log.events.append(str(stmt))
        return True


class _CountingRepo:
    def __init__(self, log: _Log, count: int) -> None:
        self._log = log
        self._count = count

    async def count_by_tier(self, user_id: Any, tier: str) -> int:  # noqa: ARG002
        self._log.events.append(f"count:{tier}")
        return self._count


async def test_lock_is_taken_before_counting() -> None:
    log = _Log()
    await goal_policy.enforce_tier_limit(
        _RecordingSession(log),  # type: ignore[arg-type]
        _CountingRepo(log, 0),  # type: ignore[arg-type]
        uuid.uuid4(),
        "focus",
    )

    lock_at = next(i for i, e in enumerate(log.events) if "pg_advisory_xact_lock" in e)
    count_at = log.events.index("count:focus")
    assert lock_at < count_at, log.events


async def test_parked_has_no_limit_and_takes_no_lock() -> None:
    log = _Log()
    await goal_policy.enforce_tier_limit(
        _RecordingSession(log),  # type: ignore[arg-type]
        _CountingRepo(log, 99),  # type: ignore[arg-type]
        uuid.uuid4(),
        "parked",
    )
    assert log.events == []


async def test_full_tier_raises_with_korean_tier_name() -> None:
    log = _Log()
    with pytest.raises(ApiError) as exc:
        await goal_policy.enforce_tier_limit(
            _RecordingSession(log),  # type: ignore[arg-type]
            _CountingRepo(log, 5),  # type: ignore[arg-type]
            uuid.uuid4(),
            "maintain",
        )
    assert exc.value.code == "GOAL_TIER_LIMIT_EXCEEDED"
    assert exc.value.field == "goalTier"
    assert "유지 목표는 최대 5개" in exc.value.message
    assert "Maintain" not in exc.value.message


# ── ② 실 동시성 — 커넥션 두 개 ───────────────────────────────────────────


async def _sessionmaker() -> AsyncIterator[Any]:
    """커밋이 필요해 `real_db_session`(롤백 격리)을 못 쓴다 — 스스로 치운다."""
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
async def test_concurrent_creates_stop_at_the_focus_limit() -> None:
    """Focus 2개인 상태에서 두 요청이 겹쳐도 하나만 들어간다 — 3개에서 멈춘다.

    세기와 넣기 사이를 일부러 벌린다(`sleep`). lock 이 없으면 두 요청이 모두 2 를 보고
    둘 다 넣어 4개가 된다 — 이 테스트가 그 교차를 실제로 만든다.
    """
    from reaction_backend.repositories.goal_repo import GoalRepo

    agen = _sessionmaker()
    sm = await anext(agen)
    uid = uuid.uuid4()
    try:
        async with sm() as s:
            s.add(User(id=uid, email=f"tier+{uid}@test.local", name="tier lock"))
            await s.flush()
            for i in range(2):
                s.add(Goal(user_id=uid, title=f"기존 {i}", category="study", goal_tier="focus"))
            await s.commit()

        async def attempt() -> str:
            async with sm() as s:
                repo = GoalRepo(s)
                try:
                    await goal_policy.enforce_tier_limit(s, repo, uid, "focus")
                except ApiError as e:
                    return e.code
                await asyncio.sleep(0.4)
                await repo.create(
                    user_id=uid,
                    title="동시 추가",
                    category="study",
                    goal_tier="focus",
                    priority_level=3,
                )
                await s.commit()
                return "created"

        results = await asyncio.wait_for(asyncio.gather(attempt(), attempt()), timeout=30)

        async with sm() as s:
            focus = await GoalRepo(s).count_by_tier(uid, "focus")
    finally:
        async with sm() as s:
            await s.execute(text("DELETE FROM goals WHERE user_id = :u"), {"u": uid})
            await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            await s.commit()
        await agen.aclose()

    assert sorted(results) == ["GOAL_TIER_LIMIT_EXCEEDED", "created"], results
    assert focus == 3


# ── ③ 배선·문구 — 라우트 ─────────────────────────────────────────────────


def _seed_goal(repo: FakeGoalRepo, *, tier: str, status: str = "active") -> Goal:
    g = Goal()
    g.id = uuid.uuid4()
    g.user_id = DEMO_USER_UUID
    g.title = f"{tier} 목표"
    g.category = "study"
    g.goal_tier = tier
    g.status = status
    g.priority_level = 3
    g.is_ultimate = False
    g.archived_at = None
    repo._items[g.id] = g
    return g


def _spy_policy(monkeypatch: Any) -> list[str]:
    calls: list[str] = []
    real = goal_policy.enforce_tier_limit

    async def spy(session: Any, repo: Any, user_id: Any, tier: str) -> None:
        calls.append(tier)
        await real(session, repo, user_id, tier)

    monkeypatch.setattr(goal_policy, "enforce_tier_limit", spy)
    return calls


def test_create_over_focus_limit_speaks_the_screen_language(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    """앱은 tier 를 '집중' 이라 부른다 — 'Focus 목표는…' 이 그대로 뜨던 문구."""
    for _ in range(3):
        _seed_goal(fake_goal_repo, tier="focus")

    res = client.post(
        "/goals",
        json={"title": "네 번째", "category": "study", "goalTier": "focus", "priorityLevel": 1},
    )

    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "GOAL_TIER_LIMIT_EXCEEDED"
    assert "집중 목표는 최대 3개" in body["message"]
    assert "Focus" not in body["message"]


def test_category_error_does_not_list_english_enum(client: TestClient) -> None:
    res = client.post(
        "/goals",
        json={"title": "목표", "category": "bogus", "goalTier": "focus", "priorityLevel": 1},
    )
    assert res.status_code == 422
    assert res.json()["field"] == "category"
    assert "study" not in res.json()["message"]


def test_every_tier_write_goes_through_the_locked_check(
    client: TestClient, fake_goal_repo: FakeGoalRepo, monkeypatch: Any
) -> None:
    """추가·tier 변경·완료 되돌리기가 모두 같은 (잠금) 판정을 탄다."""
    calls = _spy_policy(monkeypatch)

    created = client.post(
        "/goals",
        json={"title": "새 목표", "category": "study", "goalTier": "maintain", "priorityLevel": 1},
    )
    assert created.status_code == 201
    parked = _seed_goal(fake_goal_repo, tier="parked")
    assert client.patch(f"/goals/goal_{parked.id}", json={"goalTier": "focus"}).status_code == 200
    done = _seed_goal(fake_goal_repo, tier="maintain", status="completed")
    assert (
        client.post(f"/goals/goal_{done.id}/complete", json={"completed": False}).status_code == 200
    )

    assert calls == ["maintain", "focus", "maintain"]


# ── ④ 축 승격 — 멱등 판정을 lock 뒤에서 다시 읽는다 (goals-23) ─────────────


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")
async def test_concurrent_axis_promotions_make_one_goal() -> None:
    """같은 축을 두 요청이 동시에 올려도(두 번 탭, promote 와 next-cycle 동시) 목표는 하나.

    두 요청 모두 lock **전에** 축을 읽어 둔다(`next-cycle` 이 실제로 그렇다). 뒤 요청이 lock
    뒤에서 다시 읽지 않으면 "아직 승격 전" 을 그대로 믿고 같은 축으로 목표를 하나 더 만든다.
    """
    from sqlalchemy import select

    from reaction_backend.db.models.goal_node import GoalNode
    from reaction_backend.repositories.goal_repo import GoalRepo

    agen = _sessionmaker()
    sm = await anext(agen)
    uid = uuid.uuid4()
    try:
        async with sm() as s:
            s.add(User(id=uid, email=f"axis+{uid}@test.local", name="axis promote"))
            await s.flush()
            ultimate = Goal(
                user_id=uid,
                title="궁극",
                category="other",
                goal_tier="parked",
                is_ultimate=True,
            )
            s.add(ultimate)
            await s.flush()
            root = GoalNode(
                goal_id=ultimate.id,
                title="궁극",
                node_type="core",
                depth=0,
                order_index=0,
                is_leaf=False,
                tree_kind="mandala",
            )
            s.add(root)
            await s.flush()
            axis = GoalNode(
                goal_id=ultimate.id,
                parent_node_id=root.id,
                title="체력",
                node_type="subgoal",
                depth=1,
                order_index=0,
                is_leaf=False,
                tree_kind="mandala",
            )
            s.add(axis)
            await s.commit()
            axis_id = axis.id

        async def promote() -> bool:
            async with sm() as s:
                node = (
                    await s.execute(select(GoalNode).where(GoalNode.id == axis_id))
                ).scalar_one()
                _, created = await goal_policy.promote_axis(
                    s, GoalRepo(s), node=node, user_id=uid, goal_tier="focus"
                )
                await asyncio.sleep(0.3)
                await s.commit()
                return created

        results = await asyncio.wait_for(asyncio.gather(promote(), promote()), timeout=30)

        async with sm() as s:
            promoted = (
                await s.execute(
                    text("SELECT count(*) FROM goals WHERE user_id = :u AND NOT is_ultimate"),
                    {"u": uid},
                )
            ).scalar_one()
    finally:
        async with sm() as s:
            await s.execute(
                text(
                    "DELETE FROM goal_nodes WHERE goal_id IN (SELECT id FROM goals WHERE user_id = :u)"
                ),
                {"u": uid},
            )
            await s.execute(text("DELETE FROM goals WHERE user_id = :u"), {"u": uid})
            await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            await s.commit()
        await agen.aclose()

    assert sorted(results) == [False, True], results
    assert promoted == 1
