"""Planning mock fixture — #3-B 스텁용 (S06·S14·S15·S16).

데모 horizon 계획: 김민수(컴공 4학년) 페르소나 — 캡스톤·토익·코딩테스트.
Goal Structuring Orchestrator·정책 위반 롤백·LLM 자연어 편집은 #5/#6 (실구현).
스텁은 `DEMO_PLAN_ID` 한 계획만 유효한 것으로 취급한다.

⚠️ 모든 시각은 호출 시점의 KST(`now_kst()`) 기준 — 데모 계획은 항상 '이번 주'를
가리킨다. 응답 결정성을 위해 식별자는 고정값을 쓴다 (interview mock 과 동일 원칙).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from reaction_backend.schemas.common import KST, now_kst
from reaction_backend.schemas.planning import (
    ActionItem,
    BlockChange,
    Conflict,
    DayColumn,
    Plan,
    PlanDiff,
    ScheduledBlock,
    WeeklyGrid,
    WeekPlan,
)

# 데모 계획 식별자 — 스텁은 이 id 만 유효한 계획으로 취급.
DEMO_PLAN_ID = "plan_demo_0001"

# 요일 라벨 (월=0 … 일=6).
_WEEKDAY_KO: tuple[str, ...] = ("월", "화", "수", "목", "금", "토", "일")

# (actionItemId, goalId, title, estimatedMinutes, deadlineOffsetDays)
_DEMO_ACTION_ITEMS: tuple[tuple[str, str, str, int, int], ...] = (
    ("action_demo_0001", "goal_demo_capstone", "캡스톤 API 설계 문서 작성", 120, 21),
    ("action_demo_0002", "goal_demo_capstone", "캡스톤 발표자료 초안", 90, 28),
    ("action_demo_0003", "goal_demo_toeic", "토익 LC 모의고사 1회", 60, 35),
    ("action_demo_0004", "goal_demo_coding", "코딩테스트 문제 5개 풀이", 90, 14),
)

# (dayOffset, startH, startM, durMin, blockType, title, actionItemId)
_DEMO_BLOCK_SEED: tuple[tuple[int, int, int, int, str, str, str | None], ...] = (
    (0, 9, 0, 120, "focus", "캡스톤 API 설계 문서 작성", "action_demo_0001"),
    (0, 20, 0, 30, "habit", "토익 단어 30개", None),
    (1, 14, 0, 90, "focus", "코딩테스트 문제 5개 풀이", "action_demo_0004"),
    (1, 16, 0, 60, "fixed", "캡스톤 팀 미팅", None),
    (2, 10, 0, 90, "focus", "캡스톤 발표자료 초안", "action_demo_0002"),
    (2, 14, 0, 120, "focus", "캡스톤 API 설계 보완", "action_demo_0001"),
    (2, 19, 0, 60, "focus", "토익 LC 모의고사 1회", "action_demo_0003"),
    (3, 13, 0, 90, "focus", "코딩테스트 문제 5개 풀이", "action_demo_0004"),
    (3, 20, 0, 30, "habit", "토익 단어 30개", None),
    (4, 10, 0, 90, "focus", "캡스톤 발표자료 보완", "action_demo_0002"),
)

# CALENDAR_OVERLAP 충돌을 시연할 블록 (수요일 19:00 토익 LC — 캘린더 일정과 겹침).
_CONFLICT_BLOCK_INDEX = 6


def _current_week_start(today: date | None = None) -> date:
    """오늘이 속한 주의 월요일(date) — 데모 계획의 기준 주."""
    base = today or now_kst().date()
    return base - timedelta(days=base.weekday())


def _block_id(index: int) -> str:
    return f"block_demo_{index + 1:04d}"


def _build_blocks(week_start: date, *, block_status: str) -> list[ScheduledBlock]:
    """데모 블록 목록 — 기준 주(week_start) 위에 시드를 펼친다."""
    blocks: list[ScheduledBlock] = []
    for index, (offset, hh, mm, dur, btype, title, action_id) in enumerate(_DEMO_BLOCK_SEED):
        start = datetime.combine(week_start + timedelta(days=offset), time(hh, mm), tzinfo=KST)
        blocks.append(
            ScheduledBlock(
                block_id=_block_id(index),
                action_item_id=action_id,
                title=title,
                block_type=btype,
                start_at=start,
                end_at=start + timedelta(minutes=dur),
                status=block_status,
            )
        )
    return blocks


def _build_action_items(week_start: date, *, item_status: str) -> list[ActionItem]:
    return [
        ActionItem(
            action_item_id=item_id,
            goal_id=goal_id,
            title=title,
            estimated_minutes=minutes,
            deadline=week_start + timedelta(days=deadline_offset),
            status=item_status,
        )
        for item_id, goal_id, title, minutes, deadline_offset in _DEMO_ACTION_ITEMS
    ]


def _build_conflicts(blocks: list[ScheduledBlock]) -> list[Conflict]:
    conflict_block = blocks[_CONFLICT_BLOCK_INDEX]
    return [
        Conflict(
            block_id=conflict_block.block_id,
            reason="CALENDAR_OVERLAP",
            detail="이 시간에 캘린더 '스터디 모임' 일정이 겹쳐 있어요.",
        )
    ]


# 데모 계획 경고 — 수요일 집중 블록 누적이 권장치를 넘는 상황을 시연.
_DEMO_WARNINGS: list[str] = ["수요일 집중 블록이 6시간을 넘어요 — 부담이 클 수 있어요."]


def build_plan(plan_status: str) -> Plan:
    """데모 horizon 계획 한 건.

    Args:
        plan_status: ``"DRAFT"``(generate/get) 또는 ``"ACTIVE"``(approve 이후).
    """
    week_start = _current_week_start()
    block_status = "ACTIVE" if plan_status == "ACTIVE" else "DRAFT"
    blocks = _build_blocks(week_start, block_status=block_status)
    action_items = _build_action_items(week_start, item_status=plan_status)
    horizon_end = max(item.deadline for item in action_items if item.deadline is not None)
    now = now_kst()
    week = WeekPlan(
        week_start=week_start,
        workload_level="medium",
        warnings=list(_DEMO_WARNINGS),
        action_items=action_items,
        scheduled_blocks=blocks,
    )
    return Plan(
        plan_id=DEMO_PLAN_ID,
        status=plan_status,
        horizon_end=horizon_end,
        workload_level="medium",
        generated_at=now,
        approved_at=now if plan_status == "ACTIVE" else None,
        conflicts=_build_conflicts(blocks),
        warnings=list(_DEMO_WARNINGS),
        weeks=[week],
    )


def find_demo_block(block_id: str) -> ScheduledBlock | None:
    """데모 계획에서 blockId 로 블록을 찾는다 (PATCH 직접 편집용). 없으면 None."""
    blocks = _build_blocks(_current_week_start(), block_status="DRAFT")
    return next((block for block in blocks if block.block_id == block_id), None)


def build_weekly_grid(week_start: date | None = None) -> WeeklyGrid:
    """주간 그리드(S14) — 7열. 데모 블록은 기준 주에만 채워진다.

    `week_start` 미지정 시 이번 주(월요일)를 기준으로 한다.
    """
    demo_week = _current_week_start()
    week_start = week_start or demo_week
    blocks = _build_blocks(demo_week, block_status="ACTIVE") if week_start == demo_week else []
    days: list[DayColumn] = []
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        days.append(
            DayColumn(
                date=day,
                weekday=_WEEKDAY_KO[offset],
                blocks=[b for b in blocks if b.start_at.date() == day],
            )
        )
    return WeeklyGrid(
        plan_id=DEMO_PLAN_ID,
        week_start=week_start,
        week_end=week_start + timedelta(days=6),
        workload_level="medium",
        days=days,
    )


def build_plan_diff(instruction: str) -> PlanDiff:
    """자연어 수정(S16) 데모 diff — 캡스톤 블록 1건을 오후로 옮기는 수정안."""
    blocks = _build_blocks(_current_week_start(), block_status="DRAFT")
    before = blocks[0]
    after = before.model_copy(
        update={
            "start_at": before.start_at + timedelta(hours=5),
            "end_at": before.end_at + timedelta(hours=5),
        }
    )
    return PlanDiff(
        plan_id=DEMO_PLAN_ID,
        instruction=instruction,
        summary="요청을 반영해 월요일 오전 캡스톤 블록을 오후로 옮기는 수정안 1건을 만들었어요.",
        changes=[
            BlockChange(
                block_id=before.block_id,
                change_type="move",
                before=before,
                after=after,
                reason="오전 시간대 부담을 덜기 위해 오후 집중 슬롯으로 이동했어요.",
            )
        ],
        requires_approval=True,
    )
