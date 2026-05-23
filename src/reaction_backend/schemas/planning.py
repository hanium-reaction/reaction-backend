"""Planning 도메인 스키마 (api-contract §8) — Goal Structuring 초안 응답.

`POST /plans/generate` 는 `DraftPlan` 을 그대로가 아니라 본 스키마로 직렬화해
반환한다 (camelCase, KST 시간). `isActive` 는 항상 `false` — Draft Layer (§1).
"""

from __future__ import annotations

from datetime import date

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, KstDatetime


class GeneratePlanRequest(CamelModel):
    """`POST /plans/generate` 요청.

    `targetDate` 누락 시 라우터가 KST 기준 오늘 날짜로 기본값을 채운다.
    """

    target_date: date | None = None


class DraftBlockSchema(CamelModel):
    """초안 스케줄 블록 — 응답 전용. DB scheduled_blocks 와 다르며 영속화되지 않음."""

    origin: str  # "habit" | "goal"
    origin_id: str | None
    title: str
    category: str
    start_at: KstDatetime
    end_at: KstDatetime
    duration_minutes: int
    block_status: str
    source: str


class TimeWindowSchema(CamelModel):
    """단순 시간 구간 (free 블록 표현용)."""

    start_at: KstDatetime
    end_at: KstDatetime
    duration_minutes: int


class BusyBlockSchema(CamelModel):
    """가용 시간에서 제외된 점유 구간 (정책/고정 일정 원인 라벨 포함)."""

    source: str  # sleep | lunch | no_touch | late_night_block | fixed_schedule
    label: str
    start_at: KstDatetime
    end_at: KstDatetime


class GeneratePlanResponse(CamelModel):
    """`POST /plans/generate` 응답 — 비활성 초안 계획.

    `isActive` 는 항상 `false`. 활성화는 `POST /plans/{planId}/approve` 경유.
    """

    plan_id: str = Field(description="초안 식별자 (영속화 전 임시 ID)")
    target_date: date
    is_active: bool = Field(default=False, description="항상 false — Draft Layer (AGENTS §1)")
    orchestrator_state: str
    generated_at: KstDatetime
    blocks: list[DraftBlockSchema]
    free_blocks: list[TimeWindowSchema]
    busy_blocks: list[BusyBlockSchema]
    warnings: list[str]
