"""scripts.archive_demo_accounts — 대상 판정 + apply 가드 + 실 Postgres UPDATE.

여기서 고정하는 건:
1. **verifier 가 실제로 만드는 이메일**만 대상이 되는가 — 실제 Google 계정·삭제 sentinel·
   비슷하게 생긴 도메인은 절대 걸리면 안 된다.
2. `--apply` 가 dry-run 으로 본 개수 없이는, 또는 개수가 다르면 쓰기 전에 멈추는가.
3. 실 DB 에서 `load_plan`·`archive_targets` 가 대상 행의 `archived_at` 만 채우고, 다시 읽으면
   빠지는가(멱등). 롤백되는 `real_db_session` 이라 commit 하는 `run` 자체는 부르지 않는다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from scripts.archive_demo_accounts import (
    UserRow,
    archive_targets,
    build_plan,
    check_expect,
    is_stub_demo_email,
    load_plan,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.user import User
from reaction_backend.integrations.google_oauth.verifier import _stub_claims


@pytest.mark.parametrize(
    "token", ["stub", "anything", "demo:!!!", "demo:Tester-One", "demo:" + "a" * 50]
)
def test_every_stub_login_email_is_a_target(token: str) -> None:
    """verifier 가 만드는 모든 형태(고정·브라우저별·slug 절단)가 대상이어야 한다 — 형식 drift 방지."""
    assert is_stub_demo_email(_stub_claims(token).email)


@pytest.mark.parametrize(
    "email",
    [
        "someone@gmail.com",
        "demo@gmail.com",
        "demo+x@gmail.com",
        f"deleted-{uuid4()}@reaction.invalid",
        "demo+x@reaction.local.evil.com",
        "admin@reaction.local",
        "demo+@reaction.local",
        "demo+UPPER@reaction.local",
        "xdemo@reaction.local",
    ],
)
def test_non_stub_emails_are_never_targets(email: str) -> None:
    assert not is_stub_demo_email(email)


def _row(email: str) -> UserRow:
    return UserRow(uuid4(), email, "WELCOME", datetime(2026, 9, 1, tzinfo=UTC))


def test_build_plan_skips_reaction_local_rows_that_are_not_stub_shaped() -> None:
    plan = build_plan(
        [
            _row("demo@reaction.local"),
            _row("demo+abc@reaction.local"),
            _row("admin@reaction.local"),
        ]
    )
    assert [r.email for r in plan.targets] == ["demo@reaction.local", "demo+abc@reaction.local"]
    assert plan.skipped == 1


def test_apply_requires_expect() -> None:
    with pytest.raises(SystemExit, match="--expect"):
        check_expect(67, None)


def test_apply_refuses_when_count_differs() -> None:
    with pytest.raises(SystemExit, match="아무것도 바꾸지 않았다"):
        check_expect(68, 67)


def test_apply_passes_when_count_matches() -> None:
    check_expect(67, 67)
    check_expect(0, 0)


async def test_archive_targets_sets_archived_at_only_on_stub_rows(
    real_db_session: AsyncSession,
) -> None:
    # real_db_session 이 DATABASE_URL 없으면 스스로 skip 한다(CI lint-test 잡엔 postgres 가 있다).
    demo = User(email=f"demo+sqlpin-{uuid4().hex[:8]}@reaction.local", name="데모 sqlpin")
    other = User(email=f"ops-{uuid4().hex[:8]}@reaction.local", name="운영")
    real = User(email=f"real-{uuid4().hex[:8]}@example.com", name="실사용자")
    real_db_session.add_all([demo, other, real])
    await real_db_session.flush()
    mine = {demo.id, other.id, real.id}

    plan = await load_plan(real_db_session)
    targets = [t for t in plan.targets if t.id in mine]
    assert [t.id for t in targets] == [demo.id]
    assert plan.skipped >= 1  # other — @reaction.local 이지만 stub 형태가 아님

    now = datetime.now(UTC)
    assert await archive_targets(real_db_session, targets, archived_at=now) == 1

    archived = dict(
        (
            await real_db_session.execute(
                select(User.id, User.archived_at).where(User.id.in_(mine))
            )
        ).all()
    )
    assert archived[demo.id] == now
    assert archived[other.id] is None
    assert archived[real.id] is None

    # 다시 읽으면 빠진다 — 재실행은 0건(멱등).
    again = await load_plan(real_db_session)
    assert demo.id not in {t.id for t in again.targets}
    assert await archive_targets(real_db_session, targets, archived_at=now) == 0
