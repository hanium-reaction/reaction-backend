"""Plan Draft 만료 cron (#62, ADR-0005 §7.8) — 72h 미응답 Draft expired 전이 검증.

job 함수에 FakePlanDraftRepo 주입 — 룰만(LLM/DB 무관), idempotent 보장 확인.
"""

from __future__ import annotations

from datetime import date, timedelta

from reaction_backend.scheduler.expire_drafts import run_expire_stale_drafts
from reaction_backend.schemas.common import now_kst
from tests.conftest import DEMO_USER_UUID, FakePlanDraftRepo, _FakeSession


async def _seed(repo: FakePlanDraftRepo, *, expires_in_hours: int) -> object:
    return await repo.create(
        DEMO_USER_UUID,
        target_date=date(2026, 6, 22),
        horizon=None,
        ai_source="llm",
        payload={},
        expires_at=now_kst() + timedelta(hours=expires_in_hours),
    )


async def test_expire_marks_only_past_draft() -> None:
    """만료 시각이 지난 draft 만 expired, 미래 draft 는 유지."""
    repo = FakePlanDraftRepo()
    stale = await _seed(repo, expires_in_hours=-1)
    fresh = await _seed(repo, expires_in_hours=10)

    count = await run_expire_stale_drafts(_FakeSession(), repo=repo, now=now_kst())

    assert count == 1
    assert stale.status == "expired"  # type: ignore[attr-defined]
    assert fresh.status == "draft"  # type: ignore[attr-defined]


async def test_expire_is_idempotent() -> None:
    """다회 실행해도 안전 — 두 번째 실행은 전이 0건."""
    repo = FakePlanDraftRepo()
    await _seed(repo, expires_in_hours=-1)

    first = await run_expire_stale_drafts(_FakeSession(), repo=repo, now=now_kst())
    second = await run_expire_stale_drafts(_FakeSession(), repo=repo, now=now_kst())

    assert first == 1
    assert second == 0
