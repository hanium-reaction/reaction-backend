"""주간 forward 재계획 — 남은 작업을 이후 구간에 다시 배치 (룰 only, LLM 0회).

배경:
    First Plan 은 `scope=horizon` 으로 배치하되 **한 번에 최대 4주(≈한 달)** 까지만 잡는다
    (`first_plan_adapter._MAX_PLAN_WEEKS`) — 그보다 먼 구간은 주간 재계획이 이어받는 전제다.
    한 주가 지나면 그동안의 실행 결과(주간 리포트)를 바탕으로 **남은 작업을 이후로 다시
    배치**해야 한다. 이 모듈은
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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from reaction_backend.orchestrator.goal_structuring import BusyBlock, TimeInterval, pad_busy
from reaction_backend.orchestrator.plan_scheduler import (
    PlanAction,
    PlanWindow,
    schedule_actions_multiday,
)
from reaction_backend.schemas.common import KST

__all__ = [
    "GoalDeadline",
    "ReplanCandidate",
    "ReplanTuning",
    "ReplannedBlock",
    "build_forward_replan",
    "build_forward_replan_by_deadline",
    "committed_busy_from_blocks",
    "day_bounds_kst",
    "next_week_start",
]


# `committed_busy_from_blocks` 가 확정 블록에 다는 표시 — 수면·점심·수업(다른 source)과
# 구분해 하루 상한·휴식 여백의 대상을 가른다.
_COMMITTED_BLOCK_SOURCE = "scheduled_block"


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


@dataclass(frozen=True, slots=True)
class GoalDeadline:
    """후보 카드가 속한 목표의 마감 — 그 카드는 이 날까지(포함)만 배치한다 (planA-6)."""

    day: date
    goal_title: str


def next_week_start(today: date) -> date:
    """today 다음 주 월요일 (이번 주는 보존)."""
    return today + timedelta(days=(7 - today.weekday()))


def _longest_session_min(candidates: Sequence[ReplanCandidate], tuning: ReplanTuning) -> int:
    """후보들이 실제로 만들어낼 **세션**의 최대 길이 — 카드 길이가 아니다.

    하루 상한을 올리는 목적은 "세션 하나가 상한을 넘어 1차 배치에서 전부 탈락하는" 것을
    막는 것뿐이다. 그런데 스케줄러가 배치하는 단위는 카드가 아니라 `_split_minutes` 로
    쪼갠 **세션**이라(`plan_scheduler`), `focus_chunk_min` 보다 긴 카드는 어차피 나뉜다.
    카드 길이로 올리면 필요 없는 여유가 생겨 그날 총량이 프리셋을 넘는다.

    실측(폴백 튜닝 chunk=60 / cap=180): 60분 후보 6개면 하루 120분씩인데, 240분 후보 하나를
    더하면 상한이 240 으로 올라가 어떤 날은 **240분**이 쌓였다. 그 240분 후보는 4×60 으로
    쪼개지므로 상한을 올릴 이유가 애초에 없었다.
    """
    chunk = max(tuning.focus_chunk_min, 1)
    return max((min(c.estimated_minutes, chunk) for c in candidates), default=0)


def build_forward_replan(
    *,
    window_start: date,
    horizon_day: date,
    candidates: Sequence[ReplanCandidate],
    committed_busy: Sequence[BusyBlock],
    tuning: ReplanTuning,
) -> tuple[list[ReplannedBlock], list[str]]:
    """후보를 [window_start, horizon_day] 에 재배치.

    committed_busy 는 창 안의 시작/완료 블록 + 시간정책(수면/노터치) + 고정 일정을 합친
    회피 대상. (날짜별로 나눠 스케줄러 busy 콜백에 넘긴다.)

    First Plan 과 **같은 세 가지 배선**을 쓴다 (ADR-0009 D3). 예전엔 셋 다 빠져 있어서,
    첫 계획에서 지킨 것들이 매주 재계획 때 리셋됐다.

    1. `committed_min_by_day` — 그 날 **이미 확정된 집중 시간**에서 하루 상한을 이어 센다
       (#190). 빠지면 상한이 매번 0에서 시작해, 오전에 2시간 완료한 날에 상한만큼 또 얹는다.
    2. `roomy_busy_for_day` — 1차 배치는 확정 블록 앞뒤로 휴식 여백을 둔 뷰에서 고른다
       (#191). 빠지면 재배치가 진행 중인 일정에 0분 간격으로 딱 붙는다.
    3. `daily_focus_cap_min` — 후보 중 **최장 세션**보다 작지 않게 올린다. 빠지면 긴 카드가
       1차에서 전부 탈락해 상한을 무시하는 2차로 넘어간다.

    셋 다 회피 대상 중 **확정 블록(`scheduled_block`)만** 대상으로 한다 — 수면·점심·수업은
    '쉴 수 없는 시간'이지 집중 작업이 아니라, 상한에 넣으면 하루가 통째로 소진되고
    여백을 덧대면 기상 직후·수업 직후 시간이 날아간다.
    """
    busy_by_day: dict[date, list[BusyBlock]] = {}
    for b in committed_busy:
        busy_by_day.setdefault(b.interval.start.date(), []).append(b)

    committed_blocks_by_day: dict[date, list[BusyBlock]] = {
        day: [b for b in same_day if b.source == _COMMITTED_BLOCK_SOURCE]
        for day, same_day in busy_by_day.items()
    }
    committed_min_by_day: dict[date, int] = {}
    for day, committed_here in committed_blocks_by_day.items():
        total = sum(max(0, int(b.interval.duration_minutes)) for b in committed_here)
        if total:
            committed_min_by_day[day] = total

    def roomy_busy_for_day(day: date) -> list[BusyBlock]:
        same_day = busy_by_day.get(day, [])
        others = [b for b in same_day if b.source != _COMMITTED_BLOCK_SOURCE]
        padded = pad_busy(committed_blocks_by_day.get(day, []), tuning.break_min)
        return [*others, *padded]

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
        daily_focus_cap_min=max(
            tuning.daily_focus_cap_min, _longest_session_min(candidates, tuning)
        ),
        committed_min_by_day=committed_min_by_day,
        roomy_busy_for_day=roomy_busy_for_day,
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


def build_forward_replan_by_deadline(
    *,
    window_start: date,
    horizon_day: date,
    candidates: Sequence[ReplanCandidate],
    committed_busy: Sequence[BusyBlock],
    tuning: ReplanTuning,
    deadlines: Mapping[uuid.UUID, GoalDeadline],
) -> tuple[list[ReplannedBlock], list[str]]:
    """목표 마감이 지평보다 이른 카드는 **자기 마감 안에** 배치한다 (planA-6).

    `build_forward_replan` 하나로 돌리면 모든 후보가 지평 하나([window_start, horizon_day])에
    균등 분산된다 — 금요일 시험 목표의 남은 세션이 4주짜리 프로젝트 지평에 섞여 절반이 시험
    뒤로 밀렸고, 아무 경고도 없었다. 그래서 마감이 이른 목표부터 자기 마감까지를 지평으로
    배치하고, 놓인 블록을 확정 블록으로 회피 대상에 더한 뒤 다음 묶음으로 넘어간다(하루 상한·
    휴식 여백도 이어서 센다). 마감이 없거나 지평 이후인 카드는 마지막에 전체 지평으로 배치한다.

    마감 전에 다 못 넣은 세션은 **마감 뒤로 밀지 않는다** — 시험 뒤의 공부는 쓸모가 없다.
    대신 목표 이름과 마감을 짚은 경고 한 줄로 알린다(초안이라 사용자가 보고 고른다).

    마감이 **이미 지난**(재배치 시작일 전) 목표의 카드는 넣을 자리가 마감 안에 없다. 버리면
    사용자가 계속하려던 일이 말없이 사라지므로 전체 지평에 배치하되, 마감 뒤에 잡았다는 걸
    목표 단위 한 줄로 알린다(리뷰 반영) — 예전엔 아무 말 없이 마감 뒤로 흩어졌다.
    """
    # (마감일, 목표 제목) → 후보. 마감 없는 묶음은 (지평, "") — 마감 묶음은 지평보다 **엄격히**
    # 이르므로 정렬하면 항상 맨 뒤에 온다.
    groups: dict[tuple[date, str], list[ReplanCandidate]] = {}
    overdue: dict[uuid.UUID, GoalDeadline] = {}
    for c in candidates:
        d = deadlines.get(c.action_id)
        if d is not None and window_start <= d.day < horizon_day:
            key = (d.day, d.goal_title)
        else:
            key = (horizon_day, "")
            if d is not None and d.day < window_start:
                overdue[c.action_id] = d
        groups.setdefault(key, []).append(c)

    busy = list(committed_busy)
    blocks: list[ReplannedBlock] = []
    warnings: list[str] = []
    for (day, goal_title), group in sorted(groups.items(), key=lambda kv: kv[0]):
        placed, group_warnings = build_forward_replan(
            window_start=window_start,
            horizon_day=day,
            candidates=group,
            committed_busy=busy,
            tuning=tuning,
        )
        blocks.extend(placed)
        busy.extend(committed_busy_from_blocks([(b.start, b.end) for b in placed]))
        if goal_title and group_warnings:
            warnings.append(
                f"'{goal_title}' 마감({day.month}월 {day.day}일) 전에 빈 시간이 모자라 "
                f"{len(group_warnings)}개 일정은 넣지 못했어요. 마감 전 일정을 조금 비우거나 "
                "할 일을 줄여 볼까요?"
            )
        else:
            warnings.extend(group_warnings)
    # 마감이 지난 목표 — 실제로 마감 뒤에 놓인 카드가 있는 목표만, 목표마다 한 줄.
    late_goals = {overdue[b.action_id] for b in blocks if b.action_id in overdue}
    for d in sorted(late_goals, key=lambda g: (g.day, g.goal_title)):
        warnings.append(
            f"'{d.goal_title}' 마감({d.day.month}월 {d.day.day}일)이 이미 지나서, 남은 일정은 "
            "그 뒤로 잡아 뒀어요. 계속할 목표라면 마감을 새로 정해 주세요."
        )
    blocks.sort(key=lambda b: b.start)
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
