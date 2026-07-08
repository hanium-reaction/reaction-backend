"""과거 계획 중복 누적 정리 — '승인=교체'(#111/PR #113) 이전에 쌓인 데이터 소급 정리.

배경: 재생성→재승인이 반복되며 같은 날짜에 goal 카드/블록이 겹겹이 누적됐다(라이브 실측
62블록·같은 제목 ×5·같은 시각 4중첩). PR #113 이 승인 시점에 이를 막지만, 이미 쌓인
과거 데이터는 그대로 남는다. 이 스크립트는 그 fix 의 최종 상태를 소급 재현한다.

로직 (fix 의 supersede 와 동일한 판정):
  (user_id, target_date) 별로, '교체 대상'인 goal 카드만 모아 **승인 배치(created_at)** 로
  묶고, **가장 최근 배치만 남긴 뒤 이전 배치들을 보관**한다.
  - 교체 대상 = source='goal' · status='planned' · 미보관 · user_edit 블록 없음
    → 시작/완료/실패 카드(실행 이력)와 사용자가 직접 옮긴 블록은 보존.
  - created_at 은 서버 `now()`(트랜잭션 시작 시각)라 한 승인의 카드들은 값이 동일 →
    배치 경계가 결정적이다.
  - 보관: action_item.archived_at (soft delete, hard delete 금지 — AGENTS §2),
    그 카드의 'scheduled' 블록 → block_status='cancelled'.

안전:
  - 기본은 **dry-run**(아무것도 쓰지 않음). 실제 적용은 `--apply` 명시.
  - `--user-email` / `--since` 로 범위를 좁힐 수 있다(기본: 전체 사용자·전체 날짜).
  - 선택/보고 로직은 순수 함수(`plan_cleanup`)라 DB 없이 단위 테스트된다
    (`tests/test_cleanup_duplicate_plans.py`).

실행 (라이브 EC2 self-hosted runner 에서 workflow_dispatch 로):
  uv run python -m scripts.cleanup_duplicate_plans            # dry-run
  uv run python -m scripts.cleanup_duplicate_plans --apply    # 실제 적용
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models import ActionItem, ScheduledBlock, User
from reaction_backend.db.session import get_sessionmaker

# ─────────────────────────────────────────────────────────────────────────────
# 순수 선택/보고 로직 (DB 무관 — 단위 테스트 대상)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ActionRow:
    """선택 로직이 보는 action_item 의 부분."""

    id: UUID
    user_id: UUID
    target_date: date
    source: str
    status: str
    title: str
    created_at: datetime
    archived_at: datetime | None


@dataclass(frozen=True, slots=True)
class BlockRow:
    """선택 로직이 보는 scheduled_block 의 부분."""

    id: UUID
    action_item_id: UUID
    source: str
    block_status: str


@dataclass(slots=True)
class DateGroupReport:
    user_id: UUID
    target_date: date
    kept_batch_at: datetime | None
    batch_count: int
    archived_action_ids: list[UUID] = field(default_factory=list)
    cancelled_block_ids: list[UUID] = field(default_factory=list)


@dataclass(slots=True)
class CleanupPlan:
    """정리 계획 — 소급 적용 대상 id 목록 + 사람이 읽을 보고."""

    archive_action_ids: list[UUID] = field(default_factory=list)
    cancel_block_ids: list[UUID] = field(default_factory=list)
    groups: list[DateGroupReport] = field(default_factory=list)

    @property
    def touched_dates(self) -> int:
        return len(self.groups)


def plan_cleanup(actions: list[ActionRow], blocks: list[BlockRow]) -> CleanupPlan:
    """소급 정리 계획을 계산한다 (아무것도 변형하지 않는 순수 함수).

    (user_id, target_date) 별로 교체 대상 goal 카드를 승인 배치(created_at)로 묶어,
    최신 배치만 남기고 이전 배치의 카드/블록을 보관·취소 대상으로 표시한다.
    """
    blocks_by_action: dict[UUID, list[BlockRow]] = defaultdict(list)
    for b in blocks:
        blocks_by_action[b.action_item_id].append(b)
    user_edited: set[UUID] = {b.action_item_id for b in blocks if b.source == "user_edit"}

    # 교체 대상: goal · planned · 미보관 · user_edit 블록 없음.
    replaceable = [
        a
        for a in actions
        if a.source == "goal"
        and a.status == "planned"
        and a.archived_at is None
        and a.id not in user_edited
    ]

    by_date: dict[tuple[UUID, date], list[ActionRow]] = defaultdict(list)
    for a in replaceable:
        by_date[(a.user_id, a.target_date)].append(a)

    plan = CleanupPlan()
    for (user_id, target_date), group in sorted(
        by_date.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])
    ):
        batches = sorted({a.created_at for a in group})
        if len(batches) <= 1:
            continue  # 배치 1개 = 중복 누적 아님, 손대지 않음
        latest = batches[-1]
        stale = [a for a in group if a.created_at != latest]
        report = DateGroupReport(
            user_id=user_id,
            target_date=target_date,
            kept_batch_at=latest,
            batch_count=len(batches),
        )
        for a in stale:
            plan.archive_action_ids.append(a.id)
            report.archived_action_ids.append(a.id)
            # 'scheduled' 블록만 취소 — started/finished/cancelled 는 이력 보존.
            for b in blocks_by_action.get(a.id, ()):
                if b.block_status == "scheduled":
                    plan.cancel_block_ids.append(b.id)
                    report.cancelled_block_ids.append(b.id)
        plan.groups.append(report)
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# DB 러너
# ─────────────────────────────────────────────────────────────────────────────


async def _load_rows(
    session: AsyncSession, *, user_email: str | None, since: date | None
) -> tuple[list[ActionRow], list[BlockRow], dict[UUID, str]]:
    """정리 후보(planned·goal·미보관 카드)와 그 블록을 로드. 보고용 사용자 라벨도."""
    stmt = select(ActionItem).where(
        ActionItem.source == "goal",
        ActionItem.status == "planned",
        ActionItem.archived_at.is_(None),
    )
    if since is not None:
        stmt = stmt.where(ActionItem.target_date >= since)
    if user_email is not None:
        stmt = stmt.join(User, User.id == ActionItem.user_id).where(User.email == user_email)
    action_orm = list((await session.execute(stmt)).scalars().all())

    actions = [
        ActionRow(
            id=a.id,
            user_id=a.user_id,
            target_date=a.target_date,
            source=a.source,
            status=a.status,
            title=a.title,
            created_at=a.created_at,
            archived_at=a.archived_at,
        )
        for a in action_orm
    ]
    action_ids = [a.id for a in actions]

    blocks: list[BlockRow] = []
    if action_ids:
        brows = list(
            (
                await session.execute(
                    select(ScheduledBlock).where(ScheduledBlock.action_item_id.in_(action_ids))
                )
            )
            .scalars()
            .all()
        )
        blocks = [
            BlockRow(
                id=b.id,
                action_item_id=b.action_item_id,
                source=b.source,
                block_status=b.block_status,
            )
            for b in brows
        ]

    labels: dict[UUID, str] = {}
    for uid in {a.user_id for a in actions}:
        u = await session.get(User, uid)
        labels[uid] = f"{u.name} <{u.email}>" if u is not None else str(uid)
    return actions, blocks, labels


def _print_report(plan: CleanupPlan, labels: dict[UUID, str], *, apply: bool) -> None:
    head = "APPLY" if apply else "DRY-RUN (변경 없음)"
    print(f"\n=== 계획 중복 정리 [{head}] ===")
    if not plan.groups:
        print("정리할 중복 배치가 없습니다. (모든 날짜가 단일 승인 배치)")
        return
    for g in plan.groups:
        print(
            f"\n· {labels.get(g.user_id, g.user_id)}  {g.target_date}"
            f"  배치 {g.batch_count}개 → 최신({g.kept_batch_at:%m-%d %H:%M:%S})만 유지"
        )
        print(
            f"    보관 카드 {len(g.archived_action_ids)} · 취소 블록 {len(g.cancelled_block_ids)}"
        )
    print(
        f"\n합계: {plan.touched_dates}개 (user,date) 그룹 · "
        f"카드 {len(plan.archive_action_ids)} 보관 · 블록 {len(plan.cancel_block_ids)} 취소"
    )


async def run(*, apply: bool, user_email: str | None, since: date | None) -> CleanupPlan:
    sm = get_sessionmaker()
    async with sm() as session:
        actions, blocks, labels = await _load_rows(session, user_email=user_email, since=since)
        plan = plan_cleanup(actions, blocks)
        _print_report(plan, labels, apply=apply)

        if not apply or not plan.groups:
            return plan

        archived_at = datetime.now().astimezone()
        archive_ids = set(plan.archive_action_ids)
        cancel_ids = set(plan.cancel_block_ids)
        # 실제 ORM 행을 다시 로드해 변형 (soft delete only).
        for a in (
            await session.execute(select(ActionItem).where(ActionItem.id.in_(archive_ids)))
        ).scalars():
            a.archived_at = archived_at
        for b in (
            await session.execute(select(ScheduledBlock).where(ScheduledBlock.id.in_(cancel_ids)))
        ).scalars():
            if b.block_status == "scheduled":
                b.block_status = "cancelled"
        await session.commit()
        print("\n✅ 적용 완료 (soft delete — archived_at / block_status=cancelled).")
        return plan


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="과거 계획 중복 누적 소급 정리")
    p.add_argument("--apply", action="store_true", help="실제 적용 (미지정 시 dry-run)")
    p.add_argument("--user-email", default=None, help="특정 사용자만 (기본: 전체)")
    p.add_argument("--since", default=None, help="이 날짜 이후만 정리 (YYYY-MM-DD)")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    since = date.fromisoformat(args.since) if args.since else None
    asyncio.run(run(apply=args.apply, user_email=args.user_email, since=since))


if __name__ == "__main__":
    main()
