"""SQLAlchemy DeclarativeBase + 공통 mixin.

규약 (AGENTS.md):
- 모든 시간 컬럼은 timestamptz (UTC), 응답 시 KST 변환
- soft delete only (archived_at). hard delete 금지.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """모든 ORM 모델의 base. metadata는 Alembic이 참조."""


class TimestampMixin:
    """생성/수정 시각 자동 관리.

    - `created_at`: 행 INSERT 시 서버 시각 (DB-side default)
    - `updated_at`: 행 UPDATE 시마다 자동 갱신
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SoftDeleteMixin:
    """Soft delete.

    행을 물리적으로 삭제하지 않고 `archived_at`만 set.
    Repository 의 기본 조회는 `archived_at IS NULL` 필터를 자동 적용해야 함.
    """

    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
