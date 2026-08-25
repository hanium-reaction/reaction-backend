"""RecoveryAttempt — 회복 시도 (S19/S20).

흐름:
- S19 Recovery Coach Agent 가 후보별로 INSERT (user_decision='pending')
- S20 Replan Review 에서 사용자 선택 → user_decision='accepted' (선택 카드) /
  'rejected' (나머지 카드)
- 결과로 새 action_item 생성 시 resulting_action_item_id 에 그 ID 기록 (혈통)

핵심:
- 원본 action_item.status (FAILED 등) 절대 변경 X — Resilience 지표 전제
- llm_fallback_used = true → heuristic fallback 적용된 경우
- recovery_duration_minutes = recovery_completed_at - recovery_started_at (v0.6 average_recovery_minutes 원본)

DB 설계서 v0.7.1 §5.16:
- user_id denormalize (v0.7)
- execution_id (이름 정렬, v0.7)
- strategy_type FK (string, v0.7.1 PK 변경)
- trigger_tag, decision_reason, recovery_started_at/completed_at, recovery_result 추가
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.execution_event import ExecutionEvent
    from reaction_backend.db.models.recovery_strategy_catalog import RecoveryStrategyCatalog
    from reaction_backend.db.models.user import User


USER_DECISION_VALUES = ("pending", "accepted", "rejected", "edited", "skipped")

# 회복을 **채택한** 결정 — resilience 분자 · replan 대상 · 새 카드 생성.
# 'edited' 는 AI 문구를 사용자가 고쳐서 수락한 것이라 accepted 와 부수효과가 같다.
# 이 상수를 안 쓰고 "accepted" 를 직접 비교하면 편집 수락이 지표·replan 에서 조용히 빠진다.
ADOPTED_DECISION_VALUES = ("accepted", "edited")

RECOVERY_RESULT_VALUES = ("completed", "abandoned", "pending")

# 회복 카드를 **성공적으로 마친** completion_status — 이때만 duration 을 기록해
# average_recovery_minutes 에 반영한다. weekly_review._SUCCESS_STATUSES 와 같은 정의
# (done/over_done). failed·partial_done 은 abandoned 로 두고 평균에서 제외한다.
RECOVERY_SUCCESS_STATUSES = ("done", "over_done")


class RecoveryAttempt(Base, TimestampMixin):
    __tablename__ = "recovery_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    # denormalize for RLS (v0.7) — DB 설계서 §5.16
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # DB 설계서 컬럼명 정렬: execution_id
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 트리거된 실패 사유 (룰 폴백 결정용) — DB 설계서 §5.16
    trigger_tag: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # UX 노출용 — DB 설계서 §5.16
    recovery_option_group: Mapped[str] = mapped_column(
        Enum(
            "DOWNSCOPE",
            "RESCHEDULE",
            "CARRY_OVER",
            "PARK",
            name="recovery_option_group",
            create_type=False,  # catalog 의 enum 재사용
        ),
        nullable=False,
    )

    # 내부 13 전략 (FK → recovery_strategy_catalog.strategy_type, string PK)
    recovery_strategy_type: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("recovery_strategy_catalog.strategy_type", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # catalog.if_then_template 에 변수 치환된 최종 텍스트
    suggested_action_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    user_decision: Mapped[str] = mapped_column(
        Enum(*USER_DECISION_VALUES, name="recovery_user_decision"),
        nullable=False,
        server_default="pending",
    )

    # 사용자 거절 사유 (선택) — DB 설계서 §5.16
    decision_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    recovery_decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 복구 실제 시작 시각 (Average Recovery Time 계산용) — DB 설계서 §5.16
    recovery_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 복구 종료 시각 — DB 설계서 §5.16
    recovery_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    recovery_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 사후 평가 — DB 설계서 §5.16
    recovery_result: Mapped[str] = mapped_column(
        Enum(*RECOVERY_RESULT_VALUES, name="recovery_result"),
        nullable=False,
        server_default="pending",
    )

    # accepted 시 생성된 새 action_item (없는 그룹: RESCHEDULE / PARK)
    resulting_action_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("action_items.id", ondelete="SET NULL"),
        nullable=True,
    )

    llm_fallback_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # ── 온라인 실험 계측 (experiment-plan-v1.md §1 P4/P5/P6) ──

    # 이 카드가 나온 생성 배치가 쓴 프롬프트 버전 — llm_runs.prompt_version 과 같은 포맷
    # (registry.PromptTemplate.version). llm_fallback_used 와 같은 범위로 배치 전체에 동일.
    prompt_version: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # L3-1 온라인 실험 배정 라벨(예: "v2"/"v3"). 배정 로직은 아직 없다 — 컬럼만 선점.
    assigned_arm: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # 카드가 API 응답으로 처음 나간 시각(노출의 근사치). 최초 1회만, 재호출로 덮어쓰지 않는다.
    first_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 재관여를 다시 찌를 시각 — 근거 대장 §2.3(A3 이탈·재관여는 별개 역량, C5/C6 랜드마크가
    # 개시를 촉발) + §3 S8("PARK/CARRY_OVER 수락 시 필수"). PARK 는 새 카드 자체를 안 만들어
    # (RESCHEDULE 과 같이 `_GROUP_TO_SOURCE` 밖) 이 필드가 없으면 미래 접점이 통째로 사라진다.
    # `orchestrator/recovery.py::re_engagement_anchor_at()` 가 채운다 — DOWNSCOPE/RESCHEDULE
    # 은 항상 NULL(이미 오늘·기존 재배치 흐름으로 접점이 있다).
    re_engagement_anchor_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── relationships ──
    user: Mapped[User] = relationship()
    execution_event: Mapped[ExecutionEvent] = relationship(back_populates="recovery_attempts")
    strategy: Mapped[RecoveryStrategyCatalog] = relationship(back_populates="attempts")
