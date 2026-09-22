"""익명화·계정 삭제 마스킹 범위 — **실 Postgres** 로 고정한다 (auth-8/9, data-3, calendar-3).

`FakePrivacyRepo` 는 호출 기록만 남겨서, 실제 UPDATE 가 어떤 컬럼을 덮는지는 아무 테스트도
보지 않았다. 그 사이에 다음이 그대로 남아 있었다:

- 암호화한 인박스 원문의 **평문 사본** — convert-to-action 이 `action_items.title` 에,
  convert-to-goal 이 `goals.title` 에 원문을 그대로 복사한다.
- 인터뷰 자유 입력 답(붙여넣은 자료 포함) — `interview_slot_answers.value` 평문 JSONB.
- 계정 삭제 뒤의 목표·할 일·습관·일정 제목, push 구독 endpoint.
- 토큰만 sentinel 로 덮이고 `revoked_at` 이 비어 "연결됨"으로 남는 캘린더 연결.

모두 UPDATE 다 — 행 수는 그대로여야 한다(hard delete 금지, AGENTS §2).
"""

from __future__ import annotations

import uuid
from datetime import date, time, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.calendar_connection import CalendarConnection
from reaction_backend.db.models.daily_brief import DailyBrief
from reaction_backend.db.models.fixed_schedule import FixedSchedule
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.habit import Habit
from reaction_backend.db.models.inbox_item import InboxItem
from reaction_backend.db.models.interview_session import InterviewSession
from reaction_backend.db.models.interview_slot_answer import InterviewSlotAnswer
from reaction_backend.db.models.notification_setting import NotificationSetting
from reaction_backend.db.models.period_summary import PeriodSummary
from reaction_backend.db.models.plan_draft import PlanDraft
from reaction_backend.db.models.user import User
from reaction_backend.repositories.privacy_repo import PrivacyRepo, anonymize_account
from reaction_backend.safety.encryption import (
    ANONYMIZED_SENTINEL,
    encrypt_inbox_text,
    encrypt_oauth_token,
)
from reaction_backend.schemas.common import now_kst

pytestmark = pytest.mark.usefixtures("real_db_session")

S = ANONYMIZED_SENTINEL
PII = "김민수 교수님 면담 010-1234-5678"
_SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "k", "auth": "a"}}


async def _seed(session: AsyncSession) -> dict[str, Any]:
    now = now_kst()
    today = date.today()
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.local",
        name="마스킹 대상",
        onboarding_state="ACTIVE",
        last_active_at=now,
    )
    session.add(user)
    await session.flush()
    uid = user.id

    goal_from_inbox = Goal(user_id=uid, title=PII, why_now=PII)
    own_goal = Goal(user_id=uid, title="토익 900", why_now="졸업 요건", first_step=None)
    session.add_all([goal_from_inbox, own_goal])
    await session.flush()
    node = GoalNode(goal_id=own_goal.id, title="LC 파트", why_text="듣기가 약해서")
    inbox = InboxItem(
        user_id=uid,
        raw_text_encrypted=encrypt_inbox_text(PII),
        promoted_goal_id=goal_from_inbox.id,
    )
    inbox_action = ActionItem(user_id=uid, title=PII, target_date=today, source="inbox")
    goal_action = ActionItem(user_id=uid, title="단어 50개", target_date=today, source="goal")
    habit = Habit(user_id=uid, title="아침 러닝")
    schedule = FixedSchedule(
        user_id=uid,
        title="○○병원 진료",
        days_of_week=["mon"],
        start_time=time(9, 0),
        end_time=time(10, 0),
    )
    interview = InterviewSession(user_id=uid, llm_model="test")
    brief = DailyBrief(
        user_id=uid, brief_date=today, headline_text=f"'{PII}' 다시 볼까요?", expires_at=now
    )
    summary = PeriodSummary(
        user_id=uid,
        period_type="weekly",
        start_date=today - timedelta(days=7),
        end_date=today,
        llm_one_liner=PII,
        failure_analysis=None,
    )
    draft = PlanDraft(user_id=uid, target_date=today, payload={"goal": PII}, expires_at=now)
    notif = NotificationSetting(user_id=uid, push_subscription=_SUB)
    calendar = CalendarConnection(
        user_id=uid,
        provider="google",
        access_token_encrypted=encrypt_oauth_token("at-original"),
        refresh_token_encrypted=encrypt_oauth_token("rt-original"),
        expires_at=now + timedelta(hours=1),
        scopes="https://www.googleapis.com/auth/calendar.freebusy",
    )
    session.add_all(
        [node, inbox, inbox_action, goal_action, habit, schedule, interview, brief, summary]
    )
    session.add_all([draft, notif, calendar])
    await session.flush()
    text_answer = InterviewSlotAnswer(
        session_id=interview.id,
        slot_key="identity.role",
        value={"type": "text", "raw": PII, "normalized": [PII]},
        is_required=True,
    )
    skipped = InterviewSlotAnswer(
        session_id=interview.id,
        slot_key="constraints.notes",
        value={"type": "text", "raw": ""},
        is_required=False,
    )
    chip = InterviewSlotAnswer(
        session_id=interview.id,
        slot_key="goals.categories",
        value={"type": "chip", "values": ["학업"]},
        is_required=True,
    )
    session.add_all([text_answer, skipped, chip])

    # 다른 사용자 — 절대 건드리면 안 된다.
    other = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@test.local", name="남", last_active_at=now)
    session.add(other)
    await session.flush()
    other_goal = Goal(user_id=other.id, title="남의 목표")
    session.add(other_goal)
    await session.flush()
    return {
        "user": user,
        "goal_from_inbox": goal_from_inbox,
        "own_goal": own_goal,
        "node": node,
        "inbox_action": inbox_action,
        "goal_action": goal_action,
        "habit": habit,
        "schedule": schedule,
        "text_answer": text_answer,
        "skipped": skipped,
        "chip": chip,
        "brief": brief,
        "summary": summary,
        "draft": draft,
        "notif": notif,
        "calendar": calendar,
        "other_goal": other_goal,
    }


async def _col(session: AsyncSession, column: Any, row_id: uuid.UUID) -> Any:
    table = column.class_
    return (await session.execute(select(column).where(table.id == row_id))).scalar_one()


async def _count(session: AsyncSession, model: Any) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def test_anonymize_masks_plaintext_copies_and_free_text(
    real_db_session: AsyncSession,
) -> None:
    s = real_db_session
    rows = await _seed(s)
    before = {m: await _count(s, m) for m in (ActionItem, Goal, InterviewSlotAnswer)}

    await PrivacyRepo(s).anonymize_user(rows["user"].id)

    # 인박스 원문의 평문 사본
    assert await _col(s, ActionItem.title, rows["inbox_action"].id) == S
    assert await _col(s, Goal.title, rows["goal_from_inbox"].id) == S
    assert await _col(s, Goal.why_now, rows["goal_from_inbox"].id) == S
    # 인터뷰 자유 입력 답 — 형태는 유지, 빈 답(건너뜀)·선택지 답은 그대로
    assert await _col(s, InterviewSlotAnswer.value, rows["text_answer"].id) == {
        "type": "text",
        "raw": S,
        "normalized": [],
    }
    assert await _col(s, InterviewSlotAnswer.value, rows["skipped"].id) == {
        "type": "text",
        "raw": "",
    }
    chip = await _col(s, InterviewSlotAnswer.value, rows["chip"].id)
    assert chip["values"] == ["학업"]
    # 캘린더 — 토큰 덮고 연결도 끊는다
    assert await _col(s, CalendarConnection.refresh_token_encrypted, rows["calendar"].id) == S
    assert await _col(s, CalendarConnection.revoked_at, rows["calendar"].id) is not None

    # 익명화는 계정을 남긴다 — 직접 만든 계획 구조·구독은 그대로
    assert await _col(s, Goal.title, rows["own_goal"].id) == "토익 900"
    assert await _col(s, ActionItem.title, rows["goal_action"].id) == "단어 50개"
    assert await _col(s, Habit.title, rows["habit"].id) == "아침 러닝"
    assert await _col(s, NotificationSetting.push_subscription, rows["notif"].id) == _SUB
    assert await _col(s, Goal.title, rows["other_goal"].id) == "남의 목표"
    # hard delete 없음
    assert {m: await _count(s, m) for m in before} == before


async def test_delete_purge_masks_remaining_text_but_keeps_rows_and_stats(
    real_db_session: AsyncSession,
) -> None:
    s = real_db_session
    rows = await _seed(s)
    status_before = await _col(s, ActionItem.status, rows["goal_action"].id)
    before = {
        m: await _count(s, m)
        for m in (ActionItem, Goal, GoalNode, Habit, FixedSchedule, PlanDraft, DailyBrief)
    }

    repo = PrivacyRepo(s)
    await repo.anonymize_user(rows["user"].id)
    await repo.purge_account_text(rows["user"].id)

    assert await _col(s, Goal.title, rows["own_goal"].id) == S
    assert await _col(s, Goal.why_now, rows["own_goal"].id) == S
    assert await _col(s, Goal.first_step, rows["own_goal"].id) is None  # 원래 비어 있던 건 그대로
    assert await _col(s, GoalNode.title, rows["node"].id) == S
    assert await _col(s, GoalNode.why_text, rows["node"].id) == S
    assert await _col(s, ActionItem.title, rows["goal_action"].id) == S
    assert await _col(s, Habit.title, rows["habit"].id) == S
    assert await _col(s, FixedSchedule.title, rows["schedule"].id) == S
    assert await _col(s, DailyBrief.headline_text, rows["brief"].id) == S
    assert await _col(s, PeriodSummary.llm_one_liner, rows["summary"].id) == S
    assert await _col(s, PeriodSummary.failure_analysis, rows["summary"].id) is None
    assert await _col(s, PlanDraft.payload, rows["draft"].id) == {}
    assert await _col(s, NotificationSetting.push_subscription, rows["notif"].id) is None

    # 통계용 상태·다른 사용자·행 수는 그대로
    assert await _col(s, ActionItem.status, rows["goal_action"].id) == status_before
    assert await _col(s, Goal.title, rows["other_goal"].id) == "남의 목표"
    assert {m: await _count(s, m) for m in before} == before


async def test_anonymize_account_revokes_calendar_and_returns_original_refresh_token(
    real_db_session: AsyncSession,
) -> None:
    """마스킹 **전에** 원래 refresh token 을 읽어 둔다 — 덮은 뒤엔 Google 권한을 회수할 수 없다."""
    s = real_db_session
    rows = await _seed(s)
    user: User = rows["user"]
    now = now_kst()

    outcome = await anonymize_account(s, user, privacy_repo=PrivacyRepo(s), now=now, delete=True)
    await s.flush()

    assert outcome.calendar_refresh_token == "rt-original"
    assert outcome.masked > 0
    assert await _col(s, CalendarConnection.revoked_at, rows["calendar"].id) is not None
    assert await _count(s, CalendarConnection) >= 1  # 행은 남는다 (soft)
    assert user.is_anonymized is True
    assert user.anonymized_at == now
    assert user.name == S
    assert user.email == f"deleted-{user.id}@reaction.invalid"
    assert user.archived_at == now
