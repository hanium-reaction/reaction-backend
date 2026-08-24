"""Goal repository — S26 (Issue #22).

규칙:
- user_id scope 자동.
- soft delete only (`archived_at`).
- Focus ≤ 3 / Maintain ≤ 5 한도는 라우터에서 `count_by_tier` 로 enforce.
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.session import get_db


class GoalRepo:
    """Goal 영속화."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active(self, user_id: UUID) -> list[Goal]:
        stmt = (
            select(Goal)
            .where(
                Goal.user_id == user_id,
                Goal.archived_at.is_(None),
            )
            .order_by(Goal.priority_level.asc(), Goal.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_nodes(self, goal_id: UUID, *, tree_kind: str = "plan") -> list[GoalNode]:
        """이 목표의 **실제 분해 트리** — 계획 승인 시 영속된 `goal_nodes`.

        보관(archived)된 노드는 뺀다: 재생성→재승인 시 이전 트리가 보관되므로, 빼지 않으면
        옛 분해와 새 분해가 한 화면에 겹쳐 나온다.
        정렬은 화면이 트리를 그대로 그릴 수 있게 depth → order_index.

        `tree_kind` 기본값이 `"plan"` 이라 기존 호출부(`GET /goals/{id}/nodes`)는 무변경으로
        안전하다 — 만다라 73칸(`tree_kind="mandala"`)이 계획 분해 트리 화면에 섞여 나오는
        오염을 막는 축(R1, `1ee508b967ba`).
        """
        stmt = (
            select(GoalNode)
            .where(
                GoalNode.goal_id == goal_id,
                GoalNode.archived_at.is_(None),
                GoalNode.tree_kind == tree_kind,
            )
            .order_by(GoalNode.depth.asc(), GoalNode.order_index.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_mandala_node(self, user_id: UUID, node_id: UUID) -> GoalNode | None:
        """user 소유 goal 아래의 만다라 노드(U9/U10) — `goal_id` 로 join 해 소유권까지 확인.

        `tree_kind='mandala'` 로 좁힌다 — 계획 트리(`tree_kind='plan'`) 노드 id 를 이 endpoint
        에 잘못 넣어도(예: 다른 endpoint 응답에서 id 를 잘못 복사) 조용히 편집되지 않는다.
        """
        stmt = (
            select(GoalNode)
            .join(Goal, GoalNode.goal_id == Goal.id)
            .where(
                GoalNode.id == node_id,
                GoalNode.tree_kind == "mandala",
                GoalNode.archived_at.is_(None),
                Goal.user_id == user_id,
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: UUID, goal_id: UUID) -> Goal | None:
        stmt = select(Goal).where(
            Goal.id == goal_id,
            Goal.user_id == user_id,
            Goal.archived_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_ultimate(self, user_id: UUID) -> Goal | None:
        """이 사용자의 궁극목표(`Goal.is_ultimate`, 사용자당 최대 1개) — 없으면 None.

        `orchestrator/ultimate_adapter.py:materialize_ultimate_goal` 이 "같은 행" 판별에
        쓰는 것과 같은 조건이다 — 주간 리포트(`GET /reviews/weekly`)가 만다라 절을 붙일 때
        이 목표 아래 `tree_kind='mandala'` 트리를 찾는 시작점으로 재사용한다(ADR-0008 §8 "E").
        """
        stmt = select(Goal).where(
            Goal.user_id == user_id, Goal.is_ultimate.is_(True), Goal.archived_at.is_(None)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def count_by_tier(self, user_id: UUID, tier: str) -> int:
        """tier 한도(Focus ≤3 / Maintain ≤5) 계산용 개수.

        **잠정(proposed) 목표는 세지 않는다** — 한도는 '동시에 몇 개를 하기로 약속했는가'
        인데, 인터뷰가 뽑았을 뿐 계획을 승인하지 않은 목표는 아직 약속이 아니다. 세면
        인터뷰만 하고 나간 사용자가 목표를 새로 못 만들면서 이유도 알 수 없다.
        """
        stmt = (
            select(func.count())
            .select_from(Goal)
            .where(
                Goal.user_id == user_id,
                Goal.goal_tier == tier,
                Goal.archived_at.is_(None),
                Goal.status != "proposed",
            )
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def create(
        self,
        user_id: UUID,
        title: str,
        category: str,
        goal_tier: str,
        priority_level: int,
        deadline: date | None = None,
        estimated_minutes: int | None = None,
    ) -> Goal:
        goal = Goal(
            user_id=user_id,
            title=title,
            category=category,
            goal_tier=goal_tier,
            priority_level=priority_level,
            deadline=deadline,
            estimated_minutes=estimated_minutes,
        )
        self._session.add(goal)
        await self._session.flush()
        await self._session.refresh(goal)
        return goal

    async def update(
        self,
        goal: Goal,
        *,
        title: str | None = None,
        deadline: date | None = None,
        priority_level: int | None = None,
        goal_tier: str | None = None,
    ) -> Goal:
        if title is not None:
            goal.title = title
        if deadline is not None:
            goal.deadline = deadline
        if priority_level is not None:
            goal.priority_level = priority_level
        if goal_tier is not None:
            goal.goal_tier = goal_tier
        await self._session.flush()
        return goal

    async def park(self, goal: Goal) -> Goal:
        """Focus → Parked 전환 (tier 변경 단축)."""
        goal.goal_tier = "parked"
        await self._session.flush()
        return goal

    async def soft_delete(self, goal: Goal) -> None:
        goal.archived_at = datetime.now(UTC)
        goal.status = "archived"
        await self._session.flush()

    async def expire_stale_proposed(self, *, before: datetime, archived_at: datetime) -> int:
        """`before` 이전에 만들어진 잠정(proposed) 목표를 일괄 보관. 반환: 보관된 행 수.

        `proposed` 는 계획을 승인하지 않은 잠정 상태(#176)인데, 도입 당시 탈출구를 **사건**
        (다음 인터뷰의 supersede)에만 달아둬서, 인터뷰 한 번 하고 안 돌아온 사용자에게는
        흡수 상태가 됐다(#178). 이 레포의 다른 과도 상태는 전부 시간 탈출구를 갖는다
        (`plan_drafts` 72h, 미회고 카드 3일) — 그 패턴에 맞춘다.

        멱등 — `status == "proposed"` + `archived_at IS NULL` 쌍이 곧 가드라 이미 보관·활성·
        완료인 행은 건드리지 않는다(`PlanDraftRepo.expire_stale` 과 같은 역할). soft 보관만
        하므로 hard delete 금지(AGENTS §2)를 지키고, `supersede_proposed_goals` 가 이미 쓰는
        것과 **같은 필드 쌍**을 써서 하위 코드는 바뀔 게 없다.

        기준은 `updated_at` 이 아니라 `created_at` 이다 — `updated_at` 은 `onupdate=func.now()`
        라서 무관한 `PATCH /goals/{id}` 한 번이 조용히 TTL 을 새로 사버리고, 그러면 경계가
        비결정적이 되어 테스트로 고정할 수 없다.
        """
        stmt = (
            update(Goal)
            .where(
                Goal.status == "proposed",
                Goal.archived_at.is_(None),
                Goal.created_at < before,
            )
            .values(status="archived", archived_at=archived_at)
        )
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]  # CursorResult (UPDATE)


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_goal_repo(session: SessionDep) -> GoalRepo:
    return GoalRepo(session)
