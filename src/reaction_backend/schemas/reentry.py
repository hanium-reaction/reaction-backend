from typing import Literal
from uuid import UUID

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, KstDatetime


class ReentryPrepare(CamelModel):
    request_id: UUID


class ReentryStart(CamelModel):
    first_step: str = Field(min_length=1, max_length=500)


class ReentryEvent(CamelModel):
    event: Literal[
        "viewed", "selected", "start_clicked", "start_failed", "first_step_completed", "leave"
    ]


class ReentryState(CamelModel):
    attempt_id: UUID
    action_id: str
    title: str
    action_status: str
    entry_kind: str
    first_step: str | None
    execution_id: str | None
    execution_status: str | None
    started_at: KstDatetime | None
    step_completed_at: KstDatetime | None
    ended_at: KstDatetime | None
    estimated_minutes: int | None = None
    elapsed_seconds: int
    can_start: bool
    blocked_reason: str | None
