"""UserConsent — 동의 기록 (S28 Privacy, Issue #23-B).

**append-only**: 동의/철회 변경마다 새 행 INSERT. 현재 상태 = consent_type 별 최신 행.
(법적 추적성 — 과거 동의 이력 보존, UPDATE/DELETE 안 함.)

consent_type:
- required  : 서비스 필수 동의
- marketing : 마케팅 수신
- research  : 연구 활용
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.user import User


CONSENT_TYPE_VALUES = ("required", "marketing", "research")


class UserConsent(Base, TimestampMixin):
    __tablename__ = "user_consents"

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

    consent_type: Mapped[str] = mapped_column(
        Enum(*CONSENT_TYPE_VALUES, name="consent_type"),
        nullable=False,
    )
    is_granted: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # ── relationships ──
    user: Mapped[User] = relationship()
