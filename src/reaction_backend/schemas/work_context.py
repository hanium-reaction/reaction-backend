from datetime import date
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator

from reaction_backend.schemas.common import CamelModel
from reaction_backend.schemas.goals import strip_invisible


class QuickAction(CamelModel):
    request_id: UUID
    title: str = Field(min_length=1, max_length=300)
    first_step: str = Field(min_length=1, max_length=500)
    attempt_minutes: int = Field(ge=1, le=120)
    goal_id: UUID | None = None

    @field_validator("title", "first_step")
    @classmethod
    def nonblank(cls, v: str) -> str:
        value = strip_invisible(v).strip()
        if not value:
            raise ValueError("빈 내용을 저장할 수 없어요.")
        return value


class ResourceInput(CamelModel):
    url: str = Field(min_length=1, max_length=2048)
    label: str = Field(min_length=1, max_length=100)

    @field_validator("url")
    @classmethod
    def safe_url(cls, v: str) -> str:
        if any(c.isspace() or ord(c) < 32 for c in v):
            raise ValueError("올바른 웹 주소를 입력해 주세요.")
        u = urlsplit(v)
        if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password:
            raise ValueError("계정 정보 없는 http 또는 https 주소만 연결해 주세요.")
        return v

    @field_validator("label")
    @classmethod
    def label_text(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("표시 이름을 입력해 주세요.")
        return v.strip()


class ResourceView(CamelModel):
    resource: ResourceInput | None


class NoteInput(CamelModel):
    memo: str = Field(max_length=300)


class NoteView(CamelModel):
    memo: str | None


Preference = Literal["smaller_step", "context_first", "rest"]


class PreferenceInput(CamelModel):
    preference: Preference | None


class ReentryEvidence(CamelModel):
    week_start: date
    week_end: date
    attempts: int
    started: int
    first_steps_reported: int
    tasks_completed: int
    preference: Preference | None
    evidence_kind: str = "self_report"
