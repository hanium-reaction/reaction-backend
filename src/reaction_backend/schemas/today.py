"""Today / Execution 도메인 스키마 (api-contract §10) — S10~S13.

Issue #19-A 범위: **조회만** (agenda + action detail). Focus 실행 로깅(start/pause/
resume/check-ins)은 #19-B (scheduled_blocks 의존).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, KstDatetime

# Quick Check-in 4칩 (S13) — execution_events.completion_status 의 종결값 4종
ExecutionCompletion = Literal["done", "partial_done", "failed", "over_done"]


class MorningBrief(CamelModel):
    """S10 상단 Morning Brief 카드 (daily_briefs). 없으면 agenda.brief=null."""

    headline: str
    big_rock_action_id: str | None
    adjustment_hints: list[str]
    fallback_used: bool


class AgendaCard(CamelModel):
    """S10 어젠다의 ActionItem 카드 (조회 표현)."""

    action_id: str
    title: str
    category: str
    status: str
    priority: int
    estimated_minutes: int
    source: str
    why_now: str | None
    first_step: str | None
    # 취소 버튼을 그릴지 FE 가 판단할 근거 (#214). **파생 필드**(DB 컬럼 아님).
    # 판정 규칙(`domain/action_cancel.py`)은 서버에만 두고 FE 는 이 값만 본다 — FE 가
    # 규칙을 복제하면 규칙이 바뀔 때 조용히 어긋난다(FE #196·#200 에서 겪은 드리프트).
    # 특히 '실행 이력 없음' 은 FE 가 받는 필드로는 계산할 수 없다.
    cancellable: bool
    # T1 미체크 배지를 그릴지 FE 가 판단할 근거 (근거 대장 §6.2, reaction-frontend#224).
    # **파생 필드** — 블록 시작 +20분이 지났는데 아직 [▶ 시작] 을 안 눌렀는가
    # (`domain/missed_check_in.py`). push 가 아니라 인앱 배지용 신호다 — 배지를 몇 번
    # 보여줄지·언제 지울지는 FE 책임, 서버는 "지금 미체크인가"만 알려준다.
    missed_check_in: bool


class AgendaHabit(CamelModel):
    """S10 습관 row — 이번 주 habit_instance 진행."""

    instance_id: str
    habit_id: str
    title: str
    target_count: int
    done_count: int


class AgendaFixedSchedule(CamelModel):
    """S10 고정 일정 row — 오늘 요일에 걸린 것."""

    schedule_id: str
    title: str
    start_time: str  # HH:MM
    end_time: str  # HH:MM


class TodayAgenda(CamelModel):
    """GET /today/agenda 응답 — 단일 조회 (daily_brief + cards + habits + fixed)."""

    date: str  # YYYY-MM-DD (KST)
    brief: MorningBrief | None
    cards: list[AgendaCard]
    habits: list[AgendaHabit]
    fixed_schedules: list[AgendaFixedSchedule]


class MorningBriefDraft(CamelModel):
    """LLM Structured Output — `aiClient.run("brief/morning_brief")` 응답 (#19-C cron).

    Sequential brief agent. 룰 fallback 도 동일 schema 로 반환. snake↔camel: prompt 는
    `headline_ko` 등 snake 로 출력하나 CamelModel populate_by_name 으로 흡수.

    `headline_ko` 길이 상한(#224): 프롬프트의 70~120자 규칙이 문서로만 있고 강제가 없어
    154자 관측 — FE 히어로 카드가 세로로 밀려 아래 지표를 밀어냈다. 프롬프트 상한(120자)
    + 여유 20자 = 140. 초과하면 Structured Output 검증 실패 → Tool Executor 가 재시도 후
    룰 fallback 으로 내려간다(룰 헤드라인은 항상 짧다).
    """

    headline_ko: str = Field(max_length=140)
    first_step: str = ""
    reason_why_now: str = ""
    adjustment_hints: list[str] = []


class ActionDetail(CamelModel):
    """GET /today/actions/{id} 응답 — S11 카드 상세."""

    action_id: str
    title: str
    category: str
    status: str
    priority: int
    estimated_minutes: int
    target_date: str  # YYYY-MM-DD
    source: str
    why_now: str | None
    first_step: str | None
    goal_id: str | None


class ExecutionStartResponse(CamelModel):
    """POST /today/actions/{id}/start 응답 — [▶ 시작] (#19-B).

    scheduled_block 이 없으면 즉석(ad-hoc) 블록을 생성해 연결한다 (source='user_edit').
    """

    execution_id: str
    action_id: str
    completion_status: str  # in_progress
    actual_start_at: KstDatetime


class ExecutionEventResponse(CamelModel):
    """POST /today/focus/{id}/pause·resume 응답 — 집중 세션 일시정지/재개 (#83).

    pause 는 interruption_events(user_pause) 를 열고, resume 은 그 구간을 닫아
    execution.pause_total_minutes 에 누적한다. execution 자체는 in_progress 유지.
    """

    execution_id: str
    action_item_id: str
    started_at: KstDatetime
    ended_at: KstDatetime | None
    status: str  # paused | in_progress
    pause_total_minutes: int


class CheckInRequest(CamelModel):
    """POST /today/check-ins 요청 — Quick Check-in 4칩 (S13/S17)."""

    execution_id: str
    completion_status: ExecutionCompletion
    user_rating: int | None = Field(default=None, ge=1, le=5)
    user_feedback: str | None = Field(default=None, max_length=500)


class CheckInResponse(CamelModel):
    """체크인 결과. `needs_failure_tags=True` 면 FE 는 S18(실패 사유)로 이동."""

    execution_id: str
    action_id: str
    completion_status: str
    actual_duration_minutes: int | None
    needs_failure_tags: bool
