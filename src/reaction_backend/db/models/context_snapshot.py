"""ContextSnapshot — 환경 14필드 캡처 (v0.6).

Quick Check-in 완료 시 자동 INSERT. S21 Weekly Review 의 Peak/Drain/Location 인사이트의 원본.

DB 설계서 v0.7.1 §5.18:
- user_id denormalize (v0.7)
- execution_id (이름 정렬)
- day_of_week: VARCHAR(10) mon/tue/.../sun
- location_type: home/cafe/library/school/office/transit/etc.
- weather_info VARCHAR(50) (P1)

규칙:
- overwhelm_level >= 4 → S19 에서 PARK 회복 옵션 후보
- location_type='home' + focus_level 낮음 → Environment Shift 회복 옵션 후보

현황 (2026-07) — 위는 DB 설계서 §5.18 의 **의도**이고, 아직 구현이 따라오지 않았다:
- **INSERT/SELECT 코드가 0곳**이다. 캡처는 #19-B-2 유예 중(`docs/api-contract.md` §10
  `/today/check-ins` 행 · "pause/resume + context_snapshot 캡처는 #19-B-2 후속").
- 그래서 지금 S21 peak/drain 은 이 테이블이 아니라 `execution_events.plan_start_at`
  (요일×시간대 프록시)로 계산한다 — `orchestrator/weekly_review.py`. 위 3행의 "S21 인사이트의
  원본"은 아직 사실이 아니다. 이관 시 `time_of_day` enum 5종 ↔ weekly_review 의 시간대 어휘
  정합이 필요하다.
- `estimated_energy_level` 은 **실행 1건의 그 순간 상태**다(아래 "state 1~5 척도" 4형제).
  저녁 회고의 '하루 에너지'를 여기 넣지 말 것 — `execution_id` 가 NOT NULL 이라 미체크 실행이
  0건인 날엔 저장 자체가 불가하고, N건 종결 시 같은 값을 복제하면 실행 수가 가중치로 붙어
  통계가 왜곡된다. 일별 에너지는 현재 설계에 없다(#141, `docs/api-contract.md` §11).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, SmallInteger, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.execution_event import ExecutionEvent
    from reaction_backend.db.models.user import User


TIME_OF_DAY_VALUES = ("early_morning", "morning", "afternoon", "evening", "night")

# DB 설계서 §5.18 — office (work→office 정렬)
LOCATION_TYPE_VALUES = ("home", "cafe", "library", "school", "office", "transit", "etc")

DEVICE_TYPE_VALUES = ("mobile", "desktop", "tablet")

# DB 설계서 §5.18 — VARCHAR(10) mon..sun
DAY_OF_WEEK_VALUES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ContextSnapshot(Base, TimestampMixin):
    __tablename__ = "context_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    # denormalize for RLS (v0.7) — DB 설계서 §5.18
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

    # ── when ── (시간/요일)
    time_of_day: Mapped[str] = mapped_column(
        Enum(*TIME_OF_DAY_VALUES, name="context_time_of_day"),
        nullable=False,
    )
    # DB 설계서 §5.18: VARCHAR(10) mon/tue/.../sun (PR 2-F: SmallInteger → enum 변경)
    day_of_week: Mapped[str] = mapped_column(
        Enum(*DAY_OF_WEEK_VALUES, name="context_day_of_week"),
        nullable=False,
    )

    calendar_density: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_event_gap_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── state ── (1~5 척도)
    estimated_energy_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    focus_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    overwhelm_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    noise_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    # ── environment ──
    interruption_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    location_type: Mapped[str | None] = mapped_column(
        Enum(*LOCATION_TYPE_VALUES, name="context_location_type"),
        nullable=True,
    )
    device_type: Mapped[str | None] = mapped_column(
        Enum(*DEVICE_TYPE_VALUES, name="context_device_type"),
        nullable=True,
    )

    # DB 설계서: weather_info VARCHAR(50)
    weather_info: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # 우리 개선 (ADR §4 보존) — Memory Structure 14 필드 충족
    companion_present: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # ── relationships ──
    user: Mapped[User] = relationship()
    execution_event: Mapped[ExecutionEvent] = relationship(back_populates="context_snapshots")
