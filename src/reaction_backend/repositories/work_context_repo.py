from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.reentry_attempt import ReentryAttempt
from reaction_backend.db.models.user import User
from reaction_backend.db.models.work_context import ActionResource, ExecutionNote


class WorkContextRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def lock_user(self, user_id: UUID) -> User:
        return (
            await self.session.execute(
                select(User)
                .where(User.id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

    async def goal(self, user_id: UUID, goal_id: UUID) -> Goal | None:
        return (
            await self.session.execute(
                select(Goal).where(
                    Goal.id == goal_id, Goal.user_id == user_id, Goal.archived_at.is_(None)
                )
            )
        ).scalar_one_or_none()

    async def resource(self, action_id: UUID) -> ActionResource | None:
        return await self.session.get(ActionResource, action_id, populate_existing=True)

    async def note(self, execution_id: UUID) -> ExecutionNote | None:
        return await self.session.get(ExecutionNote, execution_id, populate_existing=True)

    async def evidence(
        self, user_id: UUID, start: datetime, end: datetime
    ) -> tuple[int, int, int, int]:
        rows = (
            await self.session.execute(
                select(
                    ReentryAttempt, ExecutionEvent.completion_status, ExecutionEvent.actual_end_at
                )
                .outerjoin(ExecutionEvent, ReentryAttempt.execution_id == ExecutionEvent.id)
                .where(
                    ReentryAttempt.user_id == user_id,
                    ReentryAttempt.entry_kind.in_(("restart", "resume")),
                    ReentryAttempt.viewed_at >= start,
                    ReentryAttempt.viewed_at < end,
                )
            )
        ).all()
        completed = {
            r.execution_id
            for r, status, finished in rows
            if status in ("done", "over_done") and finished and start <= finished < end
        }
        return (
            len(rows),
            sum(r.started_at is not None and r.started_at < end for r, _, _ in rows),
            sum(r.step_completed_at is not None and r.step_completed_at < end for r, _, _ in rows),
            len(completed),
        )

    async def existing_action(self, action_id: UUID) -> ActionItem | None:
        return await self.session.get(ActionItem, action_id)

    async def add_action(self, action: ActionItem) -> None:
        self.session.add(action)
        await self.session.flush()

    async def latest_note(self, user_id: UUID, action_id: UUID) -> ExecutionNote | None:
        return (
            await self.session.execute(
                select(ExecutionNote)
                .join(ExecutionEvent, ExecutionNote.execution_id == ExecutionEvent.id)
                .where(ExecutionNote.user_id == user_id, ExecutionEvent.action_item_id == action_id)
                .order_by(ExecutionNote.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
