"""목표 category 백필 — category='other' 목표를 액션 다수결로 분류.

배경: 목표 category 는 승인 시 heaviest 목표에만 · 'other'일 때만 파생되므로
(`first_plan_adapter._apply_once` step 3.5), 계획된 적 없거나 타이밍이 어긋난 목표는
'other'로 방치된다. 주간 캘린더가 **목표 category** 로 블록 색을 매기므로(FE #109/#117),
대부분 'other'면 색이 단조로워진다(전부 '기타' 색).

이 스크립트는 category='other' 목표를, 그 목표의 **활성 action_item 들의 실카테고리
다수결(non-other)** 로 채운다. 액션이 전부 'other'거나 없으면 그대로 둔다(진짜 기타).
이미 실카테고리가 설정된 목표는 건드리지 않는다(사용자/기존 분류 보존).

안전:
  - 기본 **dry-run**(아무것도 쓰지 않음). 실제 적용은 `--apply` 명시.
  - `--user-email` 로 범위 축소 가능(기본: 전체 사용자).
  - category 컬럼 UPDATE 만(hard delete 없음, AGENTS §2).
  - 선택 로직은 순수함수(`classify_goals`)라 DB 없이 단위 테스트된다.

실행 (라이브 EC2 self-hosted runner 에서 workflow_dispatch 로):
  uv run python -m scripts.classify_goals            # dry-run
  uv run python -m scripts.classify_goals --apply    # 실제 적용
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models import ActionItem, Goal, User
from reaction_backend.db.models.goal import GOAL_CATEGORY_VALUES
from reaction_backend.db.session import get_sessionmaker

# ─────────────────────────────────────────────────────────────────────────────
# 순수 선택 로직 (DB 무관 — 단위 테스트 대상)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GoalRow:
    id: UUID
    user_id: UUID
    title: str
    category: str


@dataclass(frozen=True, slots=True)
class ActionCatRow:
    goal_id: UUID
    category: str


@dataclass(frozen=True, slots=True)
class GoalReclass:
    goal_id: UUID
    user_id: UUID
    title: str
    old: str
    new: str


@dataclass(slots=True)
class ClassifyPlan:
    changes: list[GoalReclass] = field(default_factory=list)


def classify_goals(goals: list[GoalRow], actions: list[ActionCatRow]) -> ClassifyPlan:
    """category='other' 목표를 액션 다수결(non-other)로 재분류할 계획을 계산한다.

    순수 함수 — 아무것도 변형하지 않는다. 이미 실카테고리인 목표, 액션이 전부 'other'거나
    없는 목표는 대상에서 제외한다. 파생값이 유효 category 가 아니면(방어) 건너뛴다.
    """
    real_cats_by_goal: dict[UUID, list[str]] = defaultdict(list)
    for a in actions:
        if a.category != "other":
            real_cats_by_goal[a.goal_id].append(a.category)

    plan = ClassifyPlan()
    for g in sorted(goals, key=lambda x: (str(x.user_id), x.title)):
        if g.category != "other":
            continue  # 이미 분류됨 — 덮어쓰지 않음
        cats = real_cats_by_goal.get(g.id, [])
        if not cats:
            continue  # 파생 근거 없음 → 진짜 '기타' 로 유지
        new = Counter(cats).most_common(1)[0][0]
        if new not in GOAL_CATEGORY_VALUES or new == "other":
            continue  # 방어 — 유효한 실카테고리만
        plan.changes.append(GoalReclass(g.id, g.user_id, g.title, g.category, new))
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# DB 러너
# ─────────────────────────────────────────────────────────────────────────────


async def _load_rows(
    session: AsyncSession, *, user_email: str | None
) -> tuple[list[GoalRow], list[ActionCatRow], dict[UUID, str]]:
    """활성 목표 + 그 목표들의 활성 액션 category + 보고용 사용자 라벨을 로드."""
    gstmt = select(Goal).where(Goal.archived_at.is_(None))
    if user_email is not None:
        gstmt = gstmt.join(User, User.id == Goal.user_id).where(User.email == user_email)
    goal_orm = list((await session.execute(gstmt)).scalars().all())
    goals = [
        GoalRow(id=g.id, user_id=g.user_id, title=g.title, category=g.category) for g in goal_orm
    ]

    goal_ids = [g.id for g in goals]
    actions: list[ActionCatRow] = []
    if goal_ids:
        astmt = select(ActionItem.goal_id, ActionItem.category).where(
            ActionItem.goal_id.in_(goal_ids),
            ActionItem.archived_at.is_(None),
        )
        for goal_id, category in (await session.execute(astmt)).all():
            if goal_id is not None:
                actions.append(ActionCatRow(goal_id=goal_id, category=category))

    labels: dict[UUID, str] = {}
    for uid in {g.user_id for g in goals}:
        u = await session.get(User, uid)
        labels[uid] = f"{u.name} <{u.email}>" if u is not None else str(uid)
    return goals, actions, labels


def _print_report(plan: ClassifyPlan, labels: dict[UUID, str], *, apply: bool) -> None:
    head = "APPLY" if apply else "DRY-RUN (변경 없음)"
    print(f"\n=== 목표 category 백필 [{head}] ===")
    if not plan.changes:
        print("재분류할 목표가 없습니다. (모두 이미 분류됐거나, 액션이 전부 'other')")
        return
    for c in plan.changes:
        print(f"· {labels.get(c.user_id, c.user_id)}  '{c.title}'  {c.old} → {c.new}")
    print(f"\n합계: {len(plan.changes)}개 목표 재분류")


async def run(*, apply: bool, user_email: str | None) -> ClassifyPlan:
    sm = get_sessionmaker()
    async with sm() as session:
        goals, actions, labels = await _load_rows(session, user_email=user_email)
        plan = classify_goals(goals, actions)
        _print_report(plan, labels, apply=apply)

        if not apply or not plan.changes:
            return plan

        new_by_id = {c.goal_id: c.new for c in plan.changes}
        for goal in (
            await session.execute(select(Goal).where(Goal.id.in_(new_by_id.keys())))
        ).scalars():
            if goal.category == "other":  # 재확인 — 그 사이 바뀌지 않았을 때만
                goal.category = new_by_id[goal.id]
        await session.commit()
        print("\n✅ 적용 완료 (goals.category UPDATE).")
        return plan


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="목표 category 백필 (액션 다수결)")
    p.add_argument("--apply", action="store_true", help="실제 적용 (미지정 시 dry-run)")
    p.add_argument("--user-email", default=None, help="특정 사용자만 (기본: 전체)")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(run(apply=args.apply, user_email=args.user_email))


if __name__ == "__main__":
    main()
