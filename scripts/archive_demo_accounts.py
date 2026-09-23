"""stub 로그인이 만든 데모 계정을 보관(soft delete)한다 — 라이브 가입자 76명 중 67명 (2026-09-17).

배경: 예전 스테이징 데모는 `AUTH_STUB_MODE` 로 돌았고, verifier(`_stub_claims`)가 브라우저마다
`demo+<id>@reaction.local` 계정을 만들었다. 그 계정들이 라이브 DB 에 그대로 남아 가입 인원
(`toggle-signups.yml` show)의 대부분을 차지한다. 배포 환경은 stub 모드를 부팅 단계에서 막으므로
(`config._forbid_auth_stub_in_deployed_envs`) 이 계정들로는 더 이상 로그인할 수도 없다.

무엇을 하는가 — `users.archived_at` 만 채운다:
- 크론(모닝 브리프·주간 리뷰·알림·습관)은 `UserRepo.list_active()` 가 보관 계정을 빼므로
  이 계정들에 대한 LLM 호출·알림이 멈춘다. 가입 인원 집계(`count_signed_up`)에서도 빠진다.
- **익명화·이메일 변경·카드/블록 정리는 하지 않는다.** 계정 삭제(`/settings/delete-account`)와
  달리 되돌릴 수 있어야 하고, 이 계정들에 쌓인 도그푸딩 데이터는 리포트 스크립트가 사용자
  보관 여부와 무관하게 집계한다 — 연구 지표를 바꾸지 않는다.
- hard delete 없음(AGENTS §2).

대상 판정: verifier 가 만드는 두 형태만 — 정확히 `demo@reaction.local` 과
`demo+<slug>@reaction.local`(slug 는 `[a-z0-9_-]{1,32}`). `@reaction.local` 이지만 이 형태가
아닌 행은 건드리지 않고 개수만 보고한다.

안전:
  - 기본은 **dry-run**(아무것도 쓰지 않음).
  - `--apply` 는 `--expect N` 이 필수다. 대상 수가 N 과 다르면 아무것도 쓰지 않고 실패한다 —
    dry-run 으로 본 집합과 실제 적용 집합이 어긋나는 것을 막는다.
  - 레포가 PUBLIC 이라 Actions 로그도 공개다 — 이메일·이름·id 는 출력하지 않고 개수만 낸다.
  - 멱등: 이미 보관된 행은 대상에서 빠진다(다시 돌리면 0건).
  - 되돌리기: 적용 시 출력하는 `archived_at` 시각으로
    `UPDATE users SET archived_at = NULL WHERE email LIKE '%@reaction.local' AND archived_at = '<시각>'`.

실행 (라이브 EC2 self-hosted runner 에서 `archive-demo-accounts.yml` 로):
  uv run python -m scripts.archive_demo_accounts                       # dry-run
  uv run python -m scripts.archive_demo_accounts --apply --expect 67   # 실제 적용
"""

from __future__ import annotations

import argparse
import asyncio
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models import User
from reaction_backend.db.session import get_sessionmaker

DEMO_DOMAIN = "@reaction.local"
FIXED_DEMO_EMAIL = "demo@reaction.local"
_DEVICE_DEMO_EMAIL = re.compile(r"demo\+[a-z0-9_-]{1,32}@reaction\.local")


def is_stub_demo_email(email: str) -> bool:
    """verifier `_stub_claims` 가 만드는 이메일인가 — 고정 계정 또는 브라우저별 계정."""
    return email == FIXED_DEMO_EMAIL or _DEVICE_DEMO_EMAIL.fullmatch(email) is not None


@dataclass(frozen=True, slots=True)
class UserRow:
    id: UUID
    email: str
    onboarding_state: str
    last_active_at: datetime


@dataclass(slots=True)
class ArchivePlan:
    targets: list[UserRow] = field(default_factory=list)
    # `@reaction.local` 이지만 stub 형태가 아니라 건드리지 않는 행 수.
    skipped: int = 0


def build_plan(rows: Sequence[UserRow]) -> ArchivePlan:
    """보관 안 된 `@reaction.local` 행들 → 대상/제외 분리 (DB 무관 — 단위 테스트 대상)."""
    plan = ArchivePlan()
    for row in rows:
        if is_stub_demo_email(row.email):
            plan.targets.append(row)
        else:
            plan.skipped += 1
    return plan


def check_expect(n_targets: int, expect: int | None) -> None:
    """`--apply` 가드 — dry-run 으로 확인한 개수와 다르면 쓰기 전에 멈춘다."""
    if expect is None:
        raise SystemExit("--apply 는 --expect N 과 함께 써야 한다 (dry-run 으로 본 대상 수).")
    if n_targets != expect:
        raise SystemExit(
            f"대상이 {n_targets}건인데 --expect {expect} 와 다르다 — 아무것도 바꾸지 않았다. "
            "dry-run 을 다시 돌려 확인할 것."
        )


def _print_plan(plan: ArchivePlan, *, apply: bool) -> None:
    head = "APPLY" if apply else "DRY-RUN (변경 없음)"
    fixed = sum(1 for r in plan.targets if r.email == FIXED_DEMO_EMAIL)
    print(f"\n=== 데모 계정 보관 [{head}] ===")
    print(
        f"대상 {len(plan.targets)}건 — 고정 시드 {fixed} · 브라우저별 {len(plan.targets) - fixed}"
    )
    if plan.targets:
        states = Counter(r.onboarding_state for r in plan.targets)
        print("  온보딩 상태: " + ", ".join(f"{s} {n}" for s, n in sorted(states.items())))
        latest = max(r.last_active_at for r in plan.targets)
        print(f"  가장 최근 접속: {latest:%Y-%m-%d %H:%M} UTC")
    print(f"`{DEMO_DOMAIN}` 이지만 stub 형태가 아니라 제외: {plan.skipped}건")


async def load_plan(session: AsyncSession) -> ArchivePlan:
    """보관 안 된 `@reaction.local` 계정을 읽어 대상/제외로 나눈다 (SELECT 만)."""
    rows = (
        await session.execute(
            select(User.id, User.email, User.onboarding_state, User.last_active_at).where(
                User.archived_at.is_(None),
                User.email.like(f"%{DEMO_DOMAIN}"),
            )
        )
    ).all()
    return build_plan([UserRow(r[0], r[1], r[2], r[3]) for r in rows])


async def archive_targets(
    session: AsyncSession, targets: Sequence[UserRow], *, archived_at: datetime
) -> int:
    """대상 행의 `archived_at` 만 채운다 — commit 은 호출자 몫. 실제로 바뀐 행 수를 돌려준다."""
    result = await session.execute(
        update(User)
        .where(User.id.in_([r.id for r in targets]), User.archived_at.is_(None))
        .values(archived_at=archived_at)
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def run(*, apply: bool, expect: int | None) -> ArchivePlan:
    async with get_sessionmaker()() as session:
        plan = await load_plan(session)
        _print_plan(plan, apply=apply)

        if not apply:
            await session.rollback()
            return plan

        check_expect(len(plan.targets), expect)
        if not plan.targets:
            print("\n보관할 계정이 없다 — 이미 정리됐다.")
            return plan

        archived_at = datetime.now(UTC)
        updated = await archive_targets(session, plan.targets, archived_at=archived_at)
        await session.commit()
        left = len((await load_plan(session)).targets)

        print(f"\n✅ 보관 완료: {updated}건 (archived_at = {archived_at.isoformat()})")
        print(f"   남은 stub 데모 계정: {left}건")
        print("   되돌리기: 위 archived_at 시각으로 archived_at=NULL (모듈 docstring 참고).")
        return plan


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="stub 로그인이 만든 데모 계정 보관(soft delete)")
    p.add_argument("--apply", action="store_true", help="실제 적용 (미지정 시 dry-run)")
    p.add_argument("--expect", type=int, default=None, help="--apply 시 필수: 예상 대상 수")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(run(apply=args.apply, expect=args.expect))


if __name__ == "__main__":
    main()
