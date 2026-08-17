"""RecoveryStrategyCatalog — 회복 전략 마스터 (v0.7, 원본 9전략 + 2026-08-17 gap-fill 4전략).

UX 4 그룹 (DOWNSCOPE / RESCHEDULE / CARRY_OVER / PARK) ↔ 내부 13 전략 분리.
같은 그룹은 동시에 1개 카드만 사용자에게 노출, 내부는 13 전략 모두 살아있어 통계/감사.

DB 설계서 v0.7.1 §5.17: **PK = strategy_type VARCHAR(30)** (enum-like 사용)
ADR 0001 §3.3 — 마스터 테이블 string PK 채택.

v0.7.1 신규 (§6.10): primary_trigger_tags JSONB — failure_tag ↔ strategy 매핑 규칙

9 전략 ↔ 트리거 매핑 (DB 시나리오 분석):
  NANO_STEP         ← AMBIGUITY, HARD_TO_START
  DOWNSCOPE_DEFAULT ← FATIGUE, PLAN_TOO_BIG
  ENVIRONMENT_SHIFT ← DISTRACTION + location=home
  CONTEXT_REWARMING ← CONTEXT_LOSS + resumed=false
  RESCHEDULE_DEFAULT← CONFLICT
  ACTIVE_RECOVERY   ← LOW_ENERGY, FATIGUE
  CARRYOVER_DEFAULT ← PRIORITY_SHIFT
  FREEZE_SLOT       ← EMERGENCY
  PARK_DEFAULT      ← overwhelm_level >= 4 (미구현 — select_strategies 가 overwhelm 을 안 받는다)

신설 4전략 (alembic 8680c4567ca6 — `docs/research/recovery-evidence-base.md` §4.1):
  TIMEBOX_REBUDGET      ← TIME_SHORTAGE, OVERRUN
  BUFFER_INSERT         ← OVERRUN
  SELF_FORGIVENESS_NANO ← AVOIDANCE, HARD_TO_START
  GOAL_RECHECK          ← AVOIDANCE, PRIORITY_SHIFT   (PARK 그룹 — 정적 태그로 PARK 를 연다)

신설 전엔 TIME_SHORTAGE/OVERRUN/AVOIDANCE 가 어떤 전략에도 안 걸렸고, PARK 그룹은 92개
계약상 가능 입력(`tests/test_recovery_selection_coverage.py`) 전부에서 0회 노출됐다.
지금은 13태그 전부 커버되고 PARK 도 GOAL_RECHECK 로 도달 가능하다 — 단, `select_strategies`
시그니처는 그대로라 PARK_DEFAULT 개별 전략(동적 조건)은 여전히 미도달이다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.recovery_attempt import RecoveryAttempt


RECOVERY_OPTION_GROUP_VALUES = ("DOWNSCOPE", "RESCHEDULE", "CARRY_OVER", "PARK")


class RecoveryStrategyCatalog(Base, TimestampMixin):
    __tablename__ = "recovery_strategy_catalog"

    # PK = string code (ADR 0001 §3.3)
    strategy_type: Mapped[str] = mapped_column(String(30), primary_key=True)

    # DB 설계서 §5.17 컬럼명 정렬
    option_group: Mapped[str] = mapped_column(
        Enum(*RECOVERY_OPTION_GROUP_VALUES, name="recovery_option_group"),
        nullable=False,
    )

    # 사용자 표시 레이블 (예: '5분 단위로 쪼개기')
    label_ko: Mapped[str] = mapped_column(String(60), nullable=False)

    # Jinja-like 템플릿. 컨텍스트 변수 (first_step, suspended_step, energy_level 등) 치환.
    if_then_template: Mapped[str] = mapped_column(Text, nullable=False)

    # 최소 회복 단위 (NANO_STEP=5, DOWNSCOPE_DEFAULT=15)
    min_recovery_unit_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("5")
    )

    # v0.7.1 신규 (§6.10): 기본 트리거 사유 enum 배열
    # 예: ["AMBIGUITY", "HARD_TO_START"] for NANO_STEP
    # 빈 배열 [] = "명시적으로 트리거 태그 없음" (동적 컨텍스트 조건만)
    primary_trigger_tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # 휴식 모드 허용 (ACTIVE_RECOVERY=true)
    allow_rest_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # 동순위 후보 중 표시 우선순위 (낮을수록 먼저) — DB 설계서 명칭 정렬
    display_priority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("100")
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    # ── relationships ──
    attempts: Mapped[list[RecoveryAttempt]] = relationship(back_populates="strategy")
