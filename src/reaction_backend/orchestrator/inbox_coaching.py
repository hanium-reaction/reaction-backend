"""Inbox 개인화 조언 규칙 엔진.

사실 계산은 서버의 사용자 소유 데이터만 사용한다. 문구는 진단하거나 단정하지 않고,
사용자가 확인 화면으로 이동해 직접 결정하도록 안내한다(HITL).
"""

from __future__ import annotations

from datetime import date, datetime

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.habit import Habit
from reaction_backend.schemas.inbox import InboxAdviceAction, InboxCoachingAdvice


def build_coaching_advice(
    *,
    goals: list[Goal],
    habits: list[Habit],
    today_actions: list[ActionItem],
    yesterday_actions: list[ActionItem],
    today: date,
    generated_at: datetime,
) -> list[InboxCoachingAdvice]:
    """우선순위가 높은 사실부터 최대 3건을 결정적으로 만든다."""
    advice: list[InboxCoachingAdvice] = []

    unfinished = [a for a in yesterday_actions if a.status not in {"done", "over_done", "archived"}]
    if unfinished:
        first = unfinished[0]
        advice.append(
            InboxCoachingAdvice(
                advice_id=f"recovery-{today.isoformat()}-{first.id}",
                category="recovery",
                title="어제 남은 일부터 가볍게 확인해 볼까요?",
                body=f"‘{first.title}’을 포함해 마치지 못한 일이 있어요. 오늘 계획과 함께 다시 살펴보세요.",
                rationale="미완료 기록을 먼저 확인하면 오늘 계획을 현실적으로 조정하기 쉬워요.",
                evidence=[f"어제 미완료 {len(unfinished)}건"],
                action=InboxAdviceAction(type="OPEN_TODAY", label="오늘 계획 보기"),
                generated_at=generated_at,
            )
        )

    planned_today = [a for a in today_actions if a.status == "planned"]
    if planned_today:
        minutes = sum(a.estimated_minutes for a in planned_today)
        first = planned_today[0]
        advice.append(
            InboxCoachingAdvice(
                advice_id=f"today-{today.isoformat()}-{first.id}",
                category="today",
                title="오늘 할 일을 한 번에 하나씩 시작해 보세요",
                body=f"우선순위가 높은 ‘{first.title}’부터 확인할 수 있어요.",
                rationale="오늘 등록된 계획 중 우선순위가 높은 항목을 기준으로 안내했어요.",
                evidence=[f"오늘 예정 {len(planned_today)}건", f"예상 {minutes}분"],
                action=InboxAdviceAction(type="OPEN_TODAY", label="오늘 계획 보기"),
                generated_at=generated_at,
            )
        )

    deadline_goals = sorted(
        (
            g
            for g in goals
            if g.status == "active" and g.deadline is not None and g.deadline >= today
        ),
        key=lambda g: (g.deadline, g.priority_level),
    )
    if deadline_goals:
        goal = deadline_goals[0]
        deadline = goal.deadline
        if deadline is None:  # generator predicate를 타입 검사기에도 명시한다.
            return advice[:3]
        days_left = (deadline - today).days
        if days_left <= 14:
            advice.append(
                InboxCoachingAdvice(
                    advice_id=f"goal-{today.isoformat()}-{goal.id}",
                    category="goal",
                    title=f"‘{goal.title}’ 마감까지 {days_left}일 남았어요",
                    body="이번 주 계획에 필요한 다음 행동이 들어 있는지 확인해 보세요.",
                    rationale="가까운 목표 마감과 현재 계획을 함께 살펴볼 시점이에요.",
                    evidence=[f"마감 {deadline.isoformat()}", f"우선순위 {goal.priority_level}"],
                    action=InboxAdviceAction(
                        type="OPEN_GOAL", label="목표 보기", target_id=f"goal_{goal.id}"
                    ),
                    generated_at=generated_at,
                )
            )

    missed_habits = sorted(
        (h for h in habits if h.consecutive_miss_weeks > 0),
        key=lambda h: (-h.consecutive_miss_weeks, h.priority_level),
    )
    if missed_habits:
        habit = missed_habits[0]
        advice.append(
            InboxCoachingAdvice(
                advice_id=f"habit-{today.isoformat()}-{habit.id}",
                category="habit",
                title=f"‘{habit.title}’ 빈도를 다시 살펴봐도 좋아요",
                body="지금의 생활 리듬에 맞는 횟수인지 주간 계획에서 확인해 보세요.",
                rationale="최근 주간 목표에 미달한 기록이 있어 부담을 조정할 수 있도록 안내했어요.",
                evidence=[
                    f"연속 미달 {habit.consecutive_miss_weeks}주",
                    f"주 {habit.frequency_per_week}회 목표",
                ],
                action=InboxAdviceAction(type="OPEN_WEEKLY_PLAN", label="주간 계획 보기"),
                generated_at=generated_at,
            )
        )

    if not advice and goals:
        goal = sorted(goals, key=lambda g: g.priority_level)[0]
        advice.append(
            InboxCoachingAdvice(
                advice_id=f"goal-focus-{today.isoformat()}-{goal.id}",
                category="goal",
                title=f"지금 집중할 목표는 ‘{goal.title}’이에요",
                body="이번 주에 이어갈 가장 작은 행동을 계획에서 확인해 보세요.",
                rationale="현재 활성 목표 중 우선순위가 가장 높은 목표를 기준으로 안내했어요.",
                evidence=[f"우선순위 {goal.priority_level}"],
                action=InboxAdviceAction(
                    type="OPEN_GOAL", label="목표 보기", target_id=f"goal_{goal.id}"
                ),
                generated_at=generated_at,
            )
        )

    return advice[:3]
