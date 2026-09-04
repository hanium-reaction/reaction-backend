"""Goal repository — S26 (Issue #22).

규칙:
- user_id scope 자동.
- soft delete only (`archived_at`).
- Focus ≤ 3 / Maintain ≤ 5 한도는 라우터에서 `count_by_tier` 로 enforce.
- commit 은 호출자 책임.
"""

from __future__ import annotations

from collections.abc import Sequence
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

    async def rule_filled_node_ids(self, node_ids: Sequence[UUID]) -> set[UUID]:
        """주어진 노드 중 **규칙이 채운**(`source='rule'`) 것만 (#454).

        자리표시자를 식별하는 유일한 단서다 — 초안 시절의 `tmp-continue` 접두사는 승인 때
        실제 UUID 로 바뀌고 보존되지 않는다. 채우고 나면 `llm` 이 되므로 두 번 안 걸린다.
        """
        if not node_ids:
            return set()
        stmt = select(GoalNode.id).where(
            GoalNode.id.in_(node_ids),
            GoalNode.source == "rule",
            GoalNode.archived_at.is_(None),
        )
        return set((await self._session.execute(stmt)).scalars().all())

    async def mark_nodes_filled(self, node_ids: Sequence[UUID]) -> int:
        """채워진 노드의 출처를 `llm` 로 바꾼다 — 컬럼의 뜻이 "누가 **채웠는가**" 다.

        같은 카드가 다음 재계획에서 다시 후보가 되지 않게 하는 것이 이 갱신의 실질이다.
        """
        if not node_ids:
            return 0
        stmt = (
            update(GoalNode)
            .where(GoalNode.id.in_(node_ids), GoalNode.source == "rule")
            .values(source="llm")
        )
        result = await self._session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

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

    async def get_plan_milestone_node(
        self, user_id: UUID, goal_id: UUID, node_id: UUID
    ) -> GoalNode | None:
        """user 소유 goal 아래의 **계획 마일스톤** 노드 — 완료 표시(ADR-0007 §3 예외)용.

        `get_mandala_node` 와 대칭이되 축이 반대다: `tree_kind='plan'` +
        `node_type='milestone'` 로 좁힌다. 만다라 칸 id 를 넣어도, 같은 계획 트리의
        subgoal/leaf id 를 넣어도 조용히 편집되지 않는다.

        `tree_kind` 조건은 **방어적 중복**이다 — `ck_goal_nodes_mandala_type` 이 만다라
        노드를 depth 별 core/subgoal/leaf 로 묶어, `node_type='milestone'` 인 만다라 행은
        애초에 INSERT 되지 않는다. 그래서 이 조건만 지워도 테스트가 안 깨진다(뮤테이션
        확인). `get_mandala_node` 와 같은 축을 명시해 두려고 남긴다.

        **leaf 를 여기서 열어주지 않는 게 핵심이다.** 세션 수행 여부는 `action_items.status`
        가 진실 소스이고(ADR-0007 §3), 노드에 두 번째 완료 표시를 두면 그 진실이 갈린다.
        마일스톤만 예외인 이유는 롤업으로 표현할 수 없는 판단("세션은 다 했는데 아직
        아니다" / "세션은 안 했지만 다른 경로로 달성했다")이 사용자 몫이기 때문이다.
        """
        stmt = (
            select(GoalNode)
            .join(Goal, GoalNode.goal_id == Goal.id)
            .where(
                GoalNode.id == node_id,
                GoalNode.goal_id == goal_id,
                GoalNode.tree_kind == "plan",
                GoalNode.node_type == "milestone",
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

        **끝낸(completed) 목표도 세지 않는다** (ADR-0007 6b) — 같은 이유의 반대편이다.
        한도(Focus≤3)는 "지금 동시에 굴리는 것" 을 제한하는 장치인데, 끝낸 목표가 자리를
        계속 잡고 있으면 성실히 완주한 사용자일수록 새 목표를 못 만든다. 보관하면 자리가
        나지만, 완료와 보관은 다른 뜻이라(끝냄 vs 치움) 완료를 보관으로 대신할 수 없다.
        """
        stmt = (
            select(func.count())
            .select_from(Goal)
            .where(
                Goal.user_id == user_id,
                Goal.goal_tier == tier,
                Goal.archived_at.is_(None),
                Goal.status.not_in(("proposed", "completed")),
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
        category: str | None = None,
        deadline: date | None = None,
        priority_level: int | None = None,
        goal_tier: str | None = None,
    ) -> Goal:
        if title is not None:
            goal.title = title
        if category is not None:
            goal.category = category
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

    async def set_completed(self, goal: Goal, *, completed: bool) -> Goal:
        """목표 완료 확정/해제 (ADR-0007 6b) — `status` 만 바꾼다.

        `archived_at` 은 건드리지 않는다. 완료는 **끝냈다**는 뜻이고 보관(soft delete)은
        **치웠다**는 뜻이라, 완료한 목표도 목록에 남아야 한다(FE 가 `status` 로 배지를 단다).

        되돌릴 수 있게 둔 건 마일스톤 완료 표시와 같은 이유다 — 오조작 복구.
        """
        goal.status = "completed" if completed else "active"
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

    async def goal_ids_with_plan(self, goal_ids: Sequence[UUID]) -> set[UUID]:
        """이 목표들 중 **계획 트리를 가진** 것의 id.

        `GET /goals` 가 카드에 "미계획" 배지를 달 때 쓴다. 카드마다 따로 묻는 N+1 을 피하려고
        id 목록을 **한 번에** 묻는다(`mandala_adapter.fetch_promoted_axis_titles` 와 같은 방식).

        ⚠️ **상태(`status`)로 판정하지 않는 이유**: 계획 승인은 인터뷰가 뽑은 목표를
        **전부** `active` 로 승격하는데(`first_plan_adapter.materialize_goals`), 계획은
        heaviest **하나**에만 만들어진다. 그래서 `status == "active"` 는 "계획이 있다" 를
        뜻하지 않는다 — 실측으로 계획 없는 active 목표가 24건 있었다.

        ⚠️ **보관된 트리는 세지 않는다.** 재승인 시 이전 트리가 보관되므로
        (`list_nodes` 주석 참고), 보관분만 있는 목표는 "이전 주기 계획은 끝났고 이번 주기
        계획은 아직" 이다 — 미계획이 맞다.
        """
        if not goal_ids:
            return set()
        stmt = select(GoalNode.goal_id).where(
            GoalNode.goal_id.in_(goal_ids),
            GoalNode.tree_kind == "plan",
            GoalNode.archived_at.is_(None),
        )
        return set((await self._session.execute(stmt)).scalars().all())


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_goal_repo(session: SessionDep) -> GoalRepo:
    return GoalRepo(session)
