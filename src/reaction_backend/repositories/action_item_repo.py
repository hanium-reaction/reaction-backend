"""ActionItem repository — S10 Today/실행 (Issue #22-B + #19-A 조회 확장).

규칙:
- user_id scope 자동.
- 원본 `action_item.status` 변경 금지 (AGENTS.md §2 — Resilience 지표 전제). 본 repo
  는 create + **read(by date/id)** + **soft delete** 만 노출. status 변경은
  execution_events 레이어(#19-B).
- `cancel` 은 `archived_at` 만 세팅한다 — **status 는 건드리지 않는다**(#214). 조회가
  전부 `archived_at IS NULL` 로 걸러 주므로 그것만으로 목록·지표에서 빠진다.
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.session import get_db


class ActionItemRepo:
    """ActionItem 영속화 — create_from_inbox + 조회(#19-A)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_date(self, user_id: UUID, target_date: date) -> list[ActionItem]:
        """오늘 어젠다 — target_date 의 활성 카드 (priority 오름차순)."""
        stmt = (
            select(ActionItem)
            .where(
                ActionItem.user_id == user_id,
                ActionItem.target_date == target_date,
                ActionItem.archived_at.is_(None),
            )
            .order_by(ActionItem.priority.asc(), ActionItem.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, user_id: UUID, action_id: UUID) -> ActionItem | None:
        stmt = select(ActionItem).where(
            ActionItem.id == action_id,
            ActionItem.user_id == user_id,
            ActionItem.archived_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_for_update(self, user_id: UUID, action_id: UUID) -> ActionItem | None:
        """`get_by_id` + 행 잠금 — 이 카드를 **변경하려는** 요청이 쓴다 (#368).

        `get_by_id` 는 락 없는 SELECT 라, `archived_at IS NULL` 을 통과한 직후 다른
        트랜잭션(계획 교체 `supersede_previous_plan`, 목표 완료)이 그 카드를 보관해도
        알 수 없다. 뒤이은 ORM UPDATE 는 `WHERE id = :id` 뿐이라 보관된 행에 그대로
        적용되고, **'보관됐는데 실행 중'인 유령 카드**가 남는다(실 Postgres 재현).

        `FOR UPDATE` 면 READ COMMITTED 에서 선행 트랜잭션의 락을 기다렸다가 **갱신된 행으로
        WHERE 를 다시 평가**한다. 그 사이 보관됐으면 `archived_at IS NULL` 에 걸려 행이
        빠지므로 여기서 None 이 나오고, 호출자는 아무것도 만들기 전에 404 로 끝낸다.
        그래서 `archived_at` 조건과 `FOR UPDATE` 는 **같은 문장에** 있어야 한다.

        ⚠️ **읽기 전용 조회에는 쓰지 말 것.** 불필요한 잠금은 대기와 교착의 씨앗이다.
        상태를 바꾸거나 카드에 매달린 행(execution_events·scheduled_blocks)을 만드는
        경로에서만 쓴다.
        """
        stmt = (
            select(ActionItem)
            .where(
                ActionItem.id == action_id,
                ActionItem.user_id == user_id,
                ActionItem.archived_at.is_(None),
            )
            .with_for_update()
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_any(self, user_id: UUID, action_id: UUID) -> ActionItem | None:
        """보관된 카드까지 포함한 조회 — 취소의 멱등 판정용 (#214).

        `get_by_id` 로 취소를 구현하면 두 번째 호출이 404 가 된다(첫 호출이 archived 로
        만들었으므로). 재시도·중복 요청이 실패로 보이지 않게 여기서 archived 도 집는다.
        """
        stmt = select(ActionItem).where(
            ActionItem.id == action_id,
            ActionItem.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_planned_without_block(self, user_id: UUID) -> list[ActionItem]:
        """활성 블록(비-cancelled)이 하나도 없는 **planned** 카드 — 미배치 백로그(읽기 전용).

        주간 forward 재계획이 '아직 캘린더에 안 올라간 남은 일'을 함께 배치할 때의 소스.
        수락했지만 아직 개별 재배치하지 않은 회복 카드(source=recovery_*, status=planned)가
        여기 포함된다. **원본 status 는 읽기만 — 변경 금지**(AGENTS §2).
        """
        has_active_block = select(ScheduledBlock.action_item_id).where(
            ScheduledBlock.user_id == user_id,
            ScheduledBlock.block_status != "cancelled",
        )
        stmt = (
            select(ActionItem)
            .where(
                ActionItem.user_id == user_id,
                ActionItem.archived_at.is_(None),
                ActionItem.status == "planned",
                ActionItem.id.not_in(has_active_block),
            )
            .order_by(ActionItem.priority.asc(), ActionItem.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def recent_done_titles(
        self, user_id: UUID, goal_id: UUID, *, limit: int = 12
    ) -> list[str]:
        """이 목표에서 **최근 끝낸** 카드 제목 (#454) — 자리표시자를 채울 진행 맥락.

        `done`·`over_done` 만 본다. `partial_done` 을 넣으면 "여기까지 했다" 가 흐려지고,
        `failed` 는 다음 단계의 근거가 아니다(실패 맥락은 별도 채널이다).
        """
        stmt = (
            select(ActionItem.title)
            .where(
                ActionItem.user_id == user_id,
                ActionItem.goal_id == goal_id,
                ActionItem.archived_at.is_(None),
                ActionItem.status.in_(("done", "over_done")),
            )
            .order_by(ActionItem.target_date.desc(), ActionItem.created_at.desc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def find_adopted_step(
        self,
        user_id: UUID,
        inbox_item_id: UUID,
        title: str,
        target_date: date,
    ) -> ActionItem | None:
        """같은 걸음의 **활성** 카드 — adopt-step 도메인 멱등의 근거 (#213).

        `(inbox_item_id, title, target_date)` 가 모두 같으면 같은 걸음이다. 활성
        (`archived_at IS NULL`) 만 보는 이유: 카드를 보관한 뒤에는 같은 걸음을 다시
        담을 수 있어야 하고, 날짜가 바뀌면 새 카드가 맞다 — 헤더 멱등(24h TTL)이
        만드는 "어제 응답 replay" 부작용이 없다.

        **호출자는 이 결과를 읽기만 할 것** — 찾은 카드의 `status`/`target_date` 를
        손보면(get-or-update) 진행 중이던 카드가 재채택 한 번에 되돌아간다(AGENTS §2).
        `tests/test_inbox_resources.py::test_reusing_the_card_does_not_touch_its_progress`
        가 그걸 고정한다.

        ⚠️ **잔여 레이스(#216)**: read-then-insert 라 두 요청이 **정확히 동시에** 도착하면
        둘 다 못 찾고 둘 다 INSERT 한다. 실사용 창은 좁다 — 한 기기의 연타는 FE 가
        in-flight 잠금으로 막고(FE #195), 새로고침·다른 기기는 이 조회가 막는다. 완전히
        없애려면 `(user_id, inbox_item_id, title, target_date)` 부분 유니크 인덱스(=
        마이그레이션, AGENTS §8) 또는 `pg_advisory_xact_lock`(선례: `safety/push_gate.py`)
        이 필요하다. 실제 중복 재발이 관측되면 그때 간다.
        """
        stmt = (
            select(ActionItem)
            .where(
                ActionItem.user_id == user_id,
                ActionItem.inbox_item_id == inbox_item_id,
                ActionItem.title == title,
                ActionItem.target_date == target_date,
                ActionItem.archived_at.is_(None),
            )
            .order_by(ActionItem.created_at.asc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def cancel(self, action: ActionItem) -> None:
        """카드 취소 = soft delete (#214).

        `archived_at` 만 세팅하고 **`status` 는 그대로 둔다**. status 를 'archived' 로
        바꾸면 AGENTS §2 의 "원본 status 는 Resilience 지표의 전제" 를 이 경로에서
        깨뜨리게 된다 — 게다가 그럴 필요도 없다. 조회 3곳이 이미 `archived_at IS NULL`
        로 거르므로 오늘 어젠다와 백로그에서 자동으로 빠진다.

        이미 취소된 카드에 다시 호출해도 안전하다(호출자가 멱등 판정).
        """
        if action.archived_at is None:
            action.archived_at = datetime.now(UTC)

    async def create_from_inbox(
        self,
        user_id: UUID,
        inbox_item_id: UUID,
        title: str,
        category: str,
        target_date: date,
    ) -> ActionItem:
        """Inbox 항목을 실행 카드(ActionItem)로 변환 (source='inbox')."""
        action = ActionItem(
            user_id=user_id,
            title=title,
            target_date=target_date,
            category=category,
            source="inbox",
            inbox_item_id=inbox_item_id,
        )
        self._session.add(action)
        await self._session.flush()
        await self._session.refresh(action)
        return action

    async def create_from_recovery(
        self,
        *,
        user_id: UUID,
        parent_action_item_id: UUID,
        title: str,
        category: str,
        source: str,
        target_date: date,
        estimated_minutes: int,
    ) -> ActionItem:
        """회복 수락 시 새 실행 카드 생성 (source='recovery_*', Issue #20-A).

        원본 카드의 status 는 변경하지 않고 `parent_action_item_id` 로 혈통만 기록한다
        (AGENTS.md §2 — Resilience 지표 전제).

        **`goal_id` 는 부모 카드에서 물려받는다** (#367). 예전엔 안 채웠는데, 그러면 이
        카드가 어느 목표에 속하는지 아무도 모른다 — 목표 스코프 조회에 안 걸려서 **어느
        목표를 완료해도 회복 카드는 안 멈췄다.** 회복은 '그 목표를 계속하는 다른 방법'이지
        목표 밖의 일이 아니다. 부모에 `goal_id` 가 없으면(inbox/manual 유래 실패 카드의
        회복) 그대로 None — 지어내지 않는다.
        """
        parent = await self._session.get(ActionItem, parent_action_item_id)
        action = ActionItem(
            user_id=user_id,
            title=title,
            target_date=target_date,
            category=category,
            source=source,
            parent_action_item_id=parent_action_item_id,
            goal_id=parent.goal_id if parent is not None else None,
            estimated_minutes=estimated_minutes,
        )
        self._session.add(action)
        await self._session.flush()
        await self._session.refresh(action)
        return action


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_action_item_repo(session: SessionDep) -> ActionItemRepo:
    return ActionItemRepo(session)
