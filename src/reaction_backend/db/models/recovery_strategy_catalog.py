"""RecoveryStrategyCatalog — 회복 전략 마스터 (v0.7, 9전략).

UX 4 그룹 (DOWNSCOPE / RESCHEDULE / CARRY_OVER / PARK) ↔ 내부 9 전략 분리.
같은 그룹은 동시에 1개 카드만 사용자에게 노출, 내부는 9 전략 모두 살아있어 통계/감사.

if_then_template 은 컨텍스트 변수 치환 템플릿 (Jinja-like):
  "만약 30분 안에 시작 못 하면, {{first_step}} 부터 5분만 해볼까요?"

9 전략 ↔ 트리거 매핑 (DB 시나리오 분석):
  NANO_STEP         ← AMBIGUITY, HARD_TO_START
  DOWNSCOPE_DEFAULT ← FATIGUE, PLAN_TOO_BIG
  ENVIRONMENT_SHIFT ← DISTRACTION + location=home
  CONTEXT_REWARMING ← CONTEXT_LOSS + resumed=false
  RESCHEDULE_DEFAULT← CONFLICT
  ACTIVE_RECOVERY   ← LOW_ENERGY, FATIGUE
  CARRYOVER_DEFAULT ← PRIORITY_SHIFT
  FREEZE_SLOT       ← EMERGENCY
  PARK_DEFAULT      ← overwhelm_level >= 4
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.recovery_attempt import RecoveryAttempt


RECOVERY_OPTION_GROUP_VALUES = ("DOWNSCOPE", "RESCHEDULE", "CARRY_OVER", "PARK")


class RecoveryStrategyCatalog(Base, TimestampMixin):
    __tablename__ = "recovery_strategy_catalog"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    strategy_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    recovery_option_group: Mapped[str] = mapped_column(
        Enum(*RECOVERY_OPTION_GROUP_VALUES, name="recovery_option_group"),
        nullable=False,
    )

    # Jinja-like 템플릿. 컨텍스트 변수 (first_step, suspended_step, energy_level 등) 치환.
    if_then_template: Mapped[str] = mapped_column(Text, nullable=False)

    default_estimate_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("5")
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))

    # ── relationships ──
    attempts: Mapped[list[RecoveryAttempt]] = relationship(back_populates="strategy")
