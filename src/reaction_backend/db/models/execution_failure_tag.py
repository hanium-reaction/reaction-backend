"""ExecutionFailureTag — 사용자가 ExecutionEvent 에 붙인 실패 사유 (최대 2개).

S18 Failure Reason 에서 칩 선택 시 INSERT. 메모는 at-rest 암호화.

DB 설계서 v0.7.1 §5.14:
- execution_id → execution_events.id (이름 정렬)
- tag_code → failure_reason_tags.tag_code (string FK, v0.7.1 PK 변경)
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.execution_event import ExecutionEvent
    from reaction_backend.db.models.failure_reason_tag import FailureReasonTag


class ExecutionFailureTag(Base, TimestampMixin):
    __tablename__ = "execution_failure_tags"

    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "tag_code",
            name="uq_execution_failure_tags_event_tag",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    # DB 설계서 컬럼명 정렬: execution_id
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # string FK (v0.7.1 마스터 테이블 PK 변경)
    tag_code: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("failure_reason_tags.tag_code", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # 자유 메모. PII 가능성 → at-rest 암호화 (실제 함수는 후속 PR).
    memo_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── relationships ──
    execution_event: Mapped[ExecutionEvent] = relationship(back_populates="failure_tags")
    failure_tag: Mapped[FailureReasonTag] = relationship(back_populates="execution_tags")
