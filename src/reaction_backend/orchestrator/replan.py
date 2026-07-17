"""주간 forward 재계획 — 남은 작업을 이후 구간에 다시 배치 (룰 only, LLM 0회).

배경:
    First Plan 은 `scope=horizon` 으로 마감까지 한 번에 배치한다. 한 주가 지나면 그동안의
    실행 결과(주간 리포트)를 바탕으로 **남은 작업을 이후로 다시 배치**해야 한다. 이 모듈은
    그 재배치의 **순수 로직**만 담는다 — DB 조회/영속화는 라우터가 맡고, 여기서는 이미 모인
    후보·회피 busy 를 받아 `plan_scheduler.schedule_actions_multiday` 로 재배치한다.

설계 결정(합의):
    - 시작점: 다음 주 월요일(이번 주는 보존, 주간 리듬).
    - 대상: 창(window) 안 **미착수 블록의 액션** + 활성 블록 없는 **planned 백로그**(수락한 회복 포함).
      과거·시작/완료된 것은 불변. 실패 원본은 미래 블록이 없어 자동 제외(회복 수락분만 재편입).
    - 중복 0: 기존 goal/node/action **재사용**, 미래 미착수 블록만 취소→교체(라우터 승인 단계).

AGENTS.md 준수:
    - §1: 산출물은 Draft. 자동 적용 금지. 원본 action_item.status 불변.
    - §2: LLM SDK 직접 import 없음.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from reaction_backend.orchestrator.goal_structuring import BusyBlock, TimeInterval
from reaction_backend.orchestrator.plan_scheduler import (
    PlanAction,
    PlanWindow,
    schedule_actions_multiday,
)
from reaction_backend.schemas.common import KST

__all__ = [
    "ReplanCandidate",
    "ReplanTuning",
    "ReplannedBlock",
    "build_forward_replan",
    "committed_busy_from_blocks",
    "day_bounds_kst",
    "next_week_start",
]


@dataclass(frozen=True, slots=True)
class ReplanCandidate:
    """재배치 단위 — 기존 ActionItem 의 투영(새로 만들지 않음)."""

    action_id: uuid.UUID
    title: str
    category: str
    estimated_minutes: int


@dataclass(frozen=True, slots=True)
class ReplanTuning:
    """스케줄러 튜닝(피크·세션·휴식·하루 상한) — outcome/기본값에서 라우터가 조립."""

    peak_windows: Sequence[PlanWindow]
    focus_chunk_min: int
    break_min: int
    daily_focus_cap_min: int


@dataclass(frozen=True, slots=True)
class ReplannedBlock:
    """재배치 결과 블록 — 기존 action 에 연결(origin_id=action_id)."""

    action_id: uuid.UUID
    title: str
    category: str
    start: datetime
    end: datetime


def next_week_start(today: date) -> date:
    """today 다음 주 월요일 (이번 주는 보존)."""
    return today + timedelta(days=(7 - today.weekday()))


def build_forward_replan(
    *,
    window_start: date,
    horizon_day: date,
    candidates: Sequence[ReplanCandidate],
    committed_busy: Sequence[BusyBlock],
    tuning: ReplanTuning,
) -> tuple[list[ReplannedBlock], list[str]]:
    """후보를 [window_start, horizon_day] 에 재배치.

    committed_busy 는 창 안의 시작/완료 블록 + 시간정책(수면/노터치)을 합친 회피 대상.
    (날짜별로 나눠 스케줄러 busy 콜백에 넘긴다.)
    """
    busy_by_day: dict[date, list[BusyBlock]] = {}
    for b in committed_busy:
        busy_by_day.setdefault(b.interval.start.date(), []).append(b)

    actions = [
        PlanAction(
            id=c.action_id,
            node_id="",
            title=c.title,
            category=c.category,
            estimated_minutes=c.estimated_minutes,
        )
        for c in candidates
    ]
    by_id = {c.action_id: c for c in candidates}

    placed, warnings = schedule_actions_multiday(
        start_day=window_start,
        horizon_day=horizon_day,
        actions=actions,
        busy_for_day=lambda day: busy_by_day.get(day, []),
        peak_windows=tuning.peak_windows,
        focus_chunk_min=tuning.focus_chunk_min,
        break_min=tuning.break_min,
        daily_focus_cap_min=tuning.daily_focus_cap_min,
    )

    blocks: list[ReplannedBlock] = []
    for pb in placed:
        cand = by_id.get(pb.origin_id) if pb.origin_id is not None else None
        if cand is None:
            continue
        blocks.append(
            ReplannedBlock(
                action_id=cand.action_id,
                title=pb.title,
                category=pb.category,
                start=pb.interval.start,
                end=pb.interval.end,
            )
        )
    return blocks, warnings


def committed_busy_from_blocks(
    intervals: Sequence[tuple[datetime, datetime]],
) -> list[BusyBlock]:
    """(start,end) 쌍들을 회피용 BusyBlock 으로 — 라우터가 committed 블록 시각을 넘긴다."""
    out: list[BusyBlock] = []
    for start, end in intervals:
        s = start.astimezone(KST)
        e = end.astimezone(KST)
        if e > s:
            out.append(BusyBlock(TimeInterval(s, e), "scheduled_block", "확정 일정"))
    return out


def day_bounds_kst(start_day: date, end_day: date) -> tuple[datetime, datetime]:
    """[start_day 00:00, (end_day+1) 00:00) KST — 재계획 창의 조회/취소 경계."""
    start_dt = datetime.combine(start_day, time(0, 0), tzinfo=KST)
    end_dt = datetime.combine(end_day + timedelta(days=1), time(0, 0), tzinfo=KST)
    return start_dt, end_dt
