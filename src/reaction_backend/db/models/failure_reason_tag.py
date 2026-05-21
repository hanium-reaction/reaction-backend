"""FailureReasonTag — 실패 사유 마스터 테이블 (13종).

S18 에서 사용자가 선택하는 칩의 원본. 라벨 변경/사유 추가는 이 테이블 UPDATE 만으로
전체 앱에 반영. is_active 토글로 특정 사유 숨김 가능.

13종 잠금 (DB 시나리오 분석):
TIME_SHORTAGE / LOW_ENERGY / HARD_TO_START / PRIORITY_SHIFT / PLAN_TOO_BIG
/ FATIGUE / AMBIGUITY / CONFLICT / OVERRUN / AVOIDANCE / DISTRACTION
/ EMERGENCY / CONTEXT_LOSS
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.execution_failure_tag import ExecutionFailureTag


class FailureReasonTag(Base, TimestampMixin):
    __tablename__ = "failure_reason_tags"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    # UPPER_SNAKE_CASE 코드 (TIME_SHORTAGE, LOW_ENERGY 등) — 클라가 매핑용으로 사용
    tag_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    # 사용자 노출 라벨 (한국어)
    label: Mapped[str] = mapped_column(String(100), nullable=False)

    # 부가 설명 (선택)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))

    # ── relationships ──
    execution_tags: Mapped[list[ExecutionFailureTag]] = relationship(back_populates="failure_tag")
