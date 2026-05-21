"""IdempotencyKey — 24h 멱등성 캐시.

API 계약 §1.7 에 명시된 5 endpoint 가 의무 사용:
- POST /reflection/batch
- POST /recovery/decisions
- POST /replan/{execution_id}/approve
- POST /calendar/events/approve-insert
- POST /reviews/habit-penalty/{habit_id}/accept

흐름:
- 클라이언트가 Idempotency-Key 헤더 전송
- 서버: (endpoint, key) 조회 → 있으면 캐시된 response 그대로 반환
- 없으면 처리 → response_cache 에 저장, expires_at = now()+24h

규칙:
- 같은 key 에 다른 request body → 409 IDEMPOTENCY_KEY_MISMATCH (request_body_hash 비교)
- expires_at 지난 행은 6h cron 으로 cleanup
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    pass


class IdempotencyKey(Base, TimestampMixin):
    __tablename__ = "idempotency_keys"

    # DB 설계서 v0.7.1 §5.29: UNIQUE (user_id, endpoint, key) — 사용자 격리
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "endpoint",
            "key",
            name="uq_idempotency_keys_user_endpoint_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    # audit 목적. SET NULL on user delete — 익명화에서 user 행이 사라져도 idempotency 기록은 유지.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)

    # sha256 hex of request body — 같은 키 다른 body 감지
    request_body_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    response_cache: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
