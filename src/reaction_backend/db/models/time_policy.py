"""TimePolicy — 시간 정책 (S07). 계획 생성의 핵심 제약.

policy_type 별 payload (discriminated union via JSONB):
- sleep             : {"start_time": "23:00", "end_time": "07:00"}      (최소 1개 활성 필수)
- lunch             : {"start_time": "12:00", "end_time": "13:00"}
- break_min         : {"min_minutes": 15}                                (카드 간 최소 휴식)
- no_touch          : {"days_of_week": [...], "start_time": ..., "end_time": ...}
- late_night_block  : {"start_time": "22:00", "blocked_categories": [...]}
- custom            : 자유 형식

규칙: Planning Agent 가 이 정책을 위반하는 ScheduledBlock 을 생성하면 트랜잭션 롤백.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Enum, ForeignKey, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.user import User


TIME_POLICY_TYPE_VALUES = (
    "sleep",
    "lunch",
    "break_min",
    "no_touch",
    "late_night_block",
    "custom",
)


class TimePolicy(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "time_policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    policy_type: Mapped[str] = mapped_column(
        Enum(*TIME_POLICY_TYPE_VALUES, name="time_policy_type"),
        nullable=False,
    )

    # policy_type 별 다른 모양. payload['type'] == policy_type 강제는 application 검증.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    # ── relationships ──
    user: Mapped[User] = relationship()
