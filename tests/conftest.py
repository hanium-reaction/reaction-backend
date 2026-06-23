"""공통 pytest fixture.

Issue #16 이후 모든 도메인 라우터(health 제외)에 `Depends(get_current_user)` 적용.
Issue #17 이후 4 도메인(time_policies / fixed_schedules / notifications) 실 DB 의존.

테스트 격리를 위해:
- `client`         : 인증 override + 4 도메인 fake repo + fake session. 일반 도메인 테스트용.
- `unauthed_client`: 인증 override 없음 (DB 의존성만 fake) — 401 분기 검증.
- `auth_client`    : 인증 override 없음 + user_repo 만 fake — `/auth/*` 흐름 (실 JWT 발급).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, time
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from reaction_backend.api.deps import get_current_user
from reaction_backend.auth.revoke import get_revoke_store
from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.daily_brief import DailyBrief
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.failure_reason_tag import FailureReasonTag
from reaction_backend.db.models.fixed_schedule import FixedSchedule
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.habit import Habit
from reaction_backend.db.models.habit_instance import HabitInstance
from reaction_backend.db.models.inbox_item import InboxItem
from reaction_backend.db.models.interview_session import InterviewSession as InterviewSessionModel
from reaction_backend.db.models.interview_slot_answer import InterviewSlotAnswer
from reaction_backend.db.models.notification_setting import NotificationSetting
from reaction_backend.db.models.period_summary import PeriodSummary
from reaction_backend.db.models.plan_draft import PlanDraft
from reaction_backend.db.models.recovery_attempt import RecoveryAttempt
from reaction_backend.db.models.recovery_strategy_catalog import RecoveryStrategyCatalog
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.time_policy import TimePolicy
from reaction_backend.db.models.user import User
from reaction_backend.db.models.user_consent import UserConsent
from reaction_backend.db.session import get_db
from reaction_backend.main import create_app
from reaction_backend.orchestrator.weekly_review import ExecutionStat, RecoveryStat
from reaction_backend.repositories.action_item_repo import get_action_item_repo
from reaction_backend.repositories.consent_repo import get_consent_repo
from reaction_backend.repositories.daily_brief_repo import get_daily_brief_repo
from reaction_backend.repositories.execution_repo import get_execution_repo
from reaction_backend.repositories.fixed_schedule_repo import get_fixed_schedule_repo
from reaction_backend.repositories.goal_repo import get_goal_repo
from reaction_backend.repositories.habit_instance_repo import get_habit_instance_repo
from reaction_backend.repositories.habit_repo import get_habit_repo
from reaction_backend.repositories.inbox_repo import get_inbox_repo
from reaction_backend.repositories.interview_repo import get_interview_repo
from reaction_backend.repositories.notification_repo import get_notification_repo
from reaction_backend.repositories.plan_draft_repo import get_plan_draft_repo
from reaction_backend.repositories.privacy_repo import get_privacy_repo
from reaction_backend.repositories.recovery_repo import get_recovery_repo
from reaction_backend.repositories.review_repo import get_review_repo
from reaction_backend.repositories.scheduled_block_repo import get_scheduled_block_repo
from reaction_backend.repositories.time_policy_repo import get_time_policy_repo
from reaction_backend.repositories.user_repo import GoogleProfile, get_user_repo

DEMO_USER_UUID = UUID("11111111-1111-4111-8111-111111111111")


def make_demo_user(*, onboarding_state: str = "ACTIVE") -> User:
    """ORM 상태 없이 만든 demo User 인스턴스.

    `onboarding_state` 는 default ACTIVE — 상태 전이 테스트는 인자로 override.
    """
    u = User()
    u.id = DEMO_USER_UUID
    u.email = "demo@reaction.local"
    u.name = "김민수"
    u.timezone = "Asia/Seoul"
    u.onboarding_state = onboarding_state
    u.tone_mode = "gentle"
    return u


def _reset_process_singletons() -> None:
    """프로세스 단위 in-memory store 들을 테스트 간 격리."""
    store = get_revoke_store()
    clear = getattr(store, "clear", None)
    if callable(clear):
        clear()


@pytest.fixture(autouse=True)
def _ensure_test_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """테스트 환경 settings — JWT_SECRET / AUTH_STUB_MODE / COLUMN_ENCRYPTION_KEY 자동.

    Inbox raw_text 암호화에 `COLUMN_ENCRYPTION_KEY` 필요 (Issue #22-B). 32-byte 고정 키.
    LLM 은 `GEMINI_API_KEY` 빈 상태 → `ProviderUnavailable` → 자동 fallback 분기.
    """
    monkeypatch.setenv(
        "JWT_SECRET",
        "test-jwt-secret-which-is-long-enough-for-hs256-aaaaaaaa",
    )
    monkeypatch.setenv("AUTH_STUB_MODE", "true")
    # urlsafe base64 of 32 zero-bytes → AES-256 키.
    monkeypatch.setenv(
        "COLUMN_ENCRYPTION_KEY",
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    )
    from reaction_backend.config import get_settings
    from reaction_backend.safety.encryption import get_cipher

    get_settings.cache_clear()
    get_cipher.cache_clear()
    yield
    get_settings.cache_clear()
    get_cipher.cache_clear()


# ───── 가짜 세션 + 결과 ─────


class _FakeResult:
    """SQLAlchemy `Result` 의 부분 stub — `.all()` / `.scalars()` / `.scalar_one*` 만."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalars(self) -> _FakeResult:
        return self

    def scalar_one_or_none(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> Any:
        # SQLAlchemy aggregate(SUM) 쿼리는 항상 1행 반환 — fake 에선 0 default.
        # `llm_budget.check` 가 `SELECT SUM(tokens)` 호출 → 빈 결과여도 0.
        if not self._rows:
            return 0
        return self._rows[0]


class _FakeSession:
    """라우터가 호출하는 session 인터페이스의 stub.

    repo 들이 모두 dependency_override 로 fake 로 교체되므로 session 은 직접 query 수행 X.
    `time_policies` prefill 의 inline select 만 fake — 항상 빈 결과 (interview 답 없음).

    `lock_acquired` 는 advisory lock(ADR-0005 §7.6) 의 `pg_try_advisory_lock` 결과를 흉내낸다
    (default True = 획득 성공). False 로 두면 동시 진입(409) 분기를 테스트할 수 있다.
    """

    def __init__(self, *, lock_acquired: bool = True) -> None:
        self.lock_acquired = lock_acquired

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:  # noqa: ARG002
        # prefill 의 inline select / advisory unlock 만 도달 — 빈 결과 반환.
        return _FakeResult([])

    async def scalar(self, stmt: Any, params: Any = None) -> Any:  # noqa: ARG002
        # user_agent_lock 의 `SELECT pg_try_advisory_lock(...)` 만 도달.
        return self.lock_acquired

    async def flush(self) -> None:
        return None

    async def refresh(self, obj: Any) -> None:  # noqa: ARG002
        return None

    def add(self, obj: Any) -> None:  # noqa: ARG002
        return None


# ───── 가짜 repository ─────


class FakeTimePolicyRepo:
    def __init__(self) -> None:
        self._items: dict[UUID, TimePolicy] = {}

    async def list_active(self, user_id: UUID) -> list[TimePolicy]:
        return [p for p in self._items.values() if p.user_id == user_id and p.archived_at is None]

    async def get_by_id(self, user_id: UUID, policy_id: UUID) -> TimePolicy | None:
        p = self._items.get(policy_id)
        if p is None or p.user_id != user_id or p.archived_at is not None:
            return None
        return p

    async def create(self, user_id: UUID, policy_type: str, payload: dict[str, Any]) -> TimePolicy:
        p = TimePolicy()
        p.id = uuid4()
        p.user_id = user_id
        p.policy_type = policy_type
        p.payload = payload
        p.is_active = True
        p.archived_at = None
        self._items[p.id] = p
        return p

    async def update(
        self,
        policy: TimePolicy,
        *,
        payload: dict[str, Any] | None = None,
        is_active: bool | None = None,
    ) -> TimePolicy:
        if payload is not None:
            policy.payload = payload
        if is_active is not None:
            policy.is_active = is_active
        return policy

    async def soft_delete(self, policy: TimePolicy) -> None:
        policy.archived_at = datetime.now(UTC)
        policy.is_active = False

    async def count_active(self, user_id: UUID) -> int:
        return len(await self.list_active(user_id))


class FakeFixedScheduleRepo:
    def __init__(self) -> None:
        self._items: dict[UUID, FixedSchedule] = {}

    async def list_active(self, user_id: UUID) -> list[FixedSchedule]:
        items = [s for s in self._items.values() if s.user_id == user_id and s.archived_at is None]
        return sorted(items, key=lambda s: s.start_time)

    async def get_by_id(self, user_id: UUID, schedule_id: UUID) -> FixedSchedule | None:
        s = self._items.get(schedule_id)
        if s is None or s.user_id != user_id or s.archived_at is not None:
            return None
        return s

    async def create(
        self,
        user_id: UUID,
        title: str,
        days_of_week: list[str],
        start_time: time,
        end_time: time,
    ) -> FixedSchedule:
        s = FixedSchedule()
        s.id = uuid4()
        s.user_id = user_id
        s.title = title
        s.days_of_week = days_of_week
        s.start_time = start_time
        s.end_time = end_time
        s.archived_at = None
        self._items[s.id] = s
        return s

    async def update(
        self,
        schedule: FixedSchedule,
        *,
        title: str | None = None,
        days_of_week: list[str] | None = None,
        start_time: time | None = None,
        end_time: time | None = None,
    ) -> FixedSchedule:
        if title is not None:
            schedule.title = title
        if days_of_week is not None:
            schedule.days_of_week = days_of_week
        if start_time is not None:
            schedule.start_time = start_time
        if end_time is not None:
            schedule.end_time = end_time
        return schedule

    async def soft_delete(self, schedule: FixedSchedule) -> None:
        schedule.archived_at = datetime.now(UTC)

    async def count_active(self, user_id: UUID) -> int:
        return len(await self.list_active(user_id))


class FakeNotificationRepo:
    def __init__(self) -> None:
        self._items: dict[UUID, NotificationSetting] = {}

    async def get_by_user(self, user_id: UUID) -> NotificationSetting | None:
        return self._items.get(user_id)

    async def get_or_create(self, user_id: UUID) -> NotificationSetting:
        existing = self._items.get(user_id)
        if existing is not None:
            return existing
        s = NotificationSetting()
        s.id = uuid4()
        s.user_id = user_id
        s.morning_brief_time = time(8, 0)
        s.evening_reflection_time = time(21, 0)
        s.pre_card_enabled = False
        s.push_subscription = None
        self._items[user_id] = s
        return s

    async def update(
        self,
        setting: NotificationSetting,
        *,
        morning_brief_time: time | None = None,
        evening_reflection_time: time | None = None,
        pre_card_enabled: bool | None = None,
    ) -> NotificationSetting:
        if morning_brief_time is not None:
            setting.morning_brief_time = morning_brief_time
        if evening_reflection_time is not None:
            setting.evening_reflection_time = evening_reflection_time
        if pre_card_enabled is not None:
            setting.pre_card_enabled = pre_card_enabled
        return setting


class FakeGoalRepo:
    """in-memory GoalRepo — Issue #22."""

    def __init__(self) -> None:
        self._items: dict[UUID, Goal] = {}

    async def list_active(self, user_id: UUID) -> list[Goal]:
        return [g for g in self._items.values() if g.user_id == user_id and g.archived_at is None]

    async def get_by_id(self, user_id: UUID, goal_id: UUID) -> Goal | None:
        g = self._items.get(goal_id)
        if g is None or g.user_id != user_id or g.archived_at is not None:
            return None
        return g

    async def count_by_tier(self, user_id: UUID, tier: str) -> int:
        return sum(
            1
            for g in self._items.values()
            if g.user_id == user_id and g.archived_at is None and g.goal_tier == tier
        )

    async def create(
        self,
        user_id: UUID,
        title: str,
        category: str,
        goal_tier: str,
        priority_level: int,
        deadline: date | None = None,
        estimated_minutes: int | None = None,
    ) -> Goal:
        g = Goal()
        g.id = uuid4()
        g.user_id = user_id
        g.title = title
        g.category = category
        g.goal_tier = goal_tier
        g.priority_level = priority_level
        g.deadline = deadline
        g.estimated_minutes = estimated_minutes
        g.status = "active"
        g.archived_at = None
        self._items[g.id] = g
        return g

    async def update(
        self,
        goal: Goal,
        *,
        title: str | None = None,
        deadline: date | None = None,
        priority_level: int | None = None,
        goal_tier: str | None = None,
    ) -> Goal:
        if title is not None:
            goal.title = title
        if deadline is not None:
            goal.deadline = deadline
        if priority_level is not None:
            goal.priority_level = priority_level
        if goal_tier is not None:
            goal.goal_tier = goal_tier
        return goal

    async def park(self, goal: Goal) -> Goal:
        goal.goal_tier = "parked"
        return goal

    async def soft_delete(self, goal: Goal) -> None:
        goal.archived_at = datetime.now(UTC)
        goal.status = "archived"


class FakeHabitRepo:
    """in-memory HabitRepo — Issue #22."""

    def __init__(self) -> None:
        self._items: dict[UUID, Habit] = {}

    async def list_active(self, user_id: UUID) -> list[Habit]:
        return [h for h in self._items.values() if h.user_id == user_id and h.archived_at is None]

    async def get_by_id(self, user_id: UUID, habit_id: UUID) -> Habit | None:
        h = self._items.get(habit_id)
        if h is None or h.user_id != user_id or h.archived_at is not None:
            return None
        return h

    async def create(
        self,
        user_id: UUID,
        title: str,
        category: str,
        frequency_per_week: int,
        minutes_per_session: int,
        time_preference: str,
        priority_level: int,
    ) -> Habit:
        h = Habit()
        h.id = uuid4()
        h.user_id = user_id
        h.title = title
        h.category = category
        h.frequency_per_week = frequency_per_week
        h.target_count = frequency_per_week
        h.minutes_per_session = minutes_per_session
        h.time_preference = time_preference
        h.priority_level = priority_level
        h.archived_at = None
        h.consecutive_miss_weeks = 0
        h.last_penalty_evaluated_at = None
        h.last_penalty_decision = None
        self._items[h.id] = h
        return h

    async def update(
        self,
        habit: Habit,
        *,
        title: str | None = None,
        frequency_per_week: int | None = None,
    ) -> Habit:
        if title is not None:
            habit.title = title
        if frequency_per_week is not None:
            habit.frequency_per_week = frequency_per_week
            habit.target_count = frequency_per_week
        return habit

    async def apply_penalty(
        self, habit: Habit, *, new_frequency: int, decided_at: datetime
    ) -> Habit:
        habit.frequency_per_week = new_frequency
        habit.target_count = new_frequency
        habit.last_penalty_decision = "accepted"
        habit.last_penalty_evaluated_at = decided_at
        habit.consecutive_miss_weeks = 0
        return habit

    async def soft_delete(self, habit: Habit) -> None:
        habit.archived_at = datetime.now(UTC)

    def seed(self, habit: Habit) -> None:
        """테스트 보조 — habit 직접 주입."""
        self._items[habit.id] = habit

    async def count_active(self, user_id: UUID) -> int:
        return len(await self.list_active(user_id))


class FakeHabitInstanceRepo:
    """in-memory HabitInstanceRepo — Issue #22.

    user scope 는 단순화 — 같은 week_start 의 모든 instance 반환. 테스트가 사용자별 habit 를
    섞어 쓰지 않으므로 충분.
    """

    def __init__(self) -> None:
        self._items: dict[UUID, HabitInstance] = {}
        self._by_habit_week: dict[tuple[UUID, date], UUID] = {}

    async def list_for_user_week(self, user_id: UUID, week_start: date) -> list[HabitInstance]:
        return [i for i in self._items.values() if i.week_start == week_start]

    async def get_for_user(self, user_id: UUID, instance_id: UUID) -> HabitInstance | None:
        return self._items.get(instance_id)

    async def list_recent_for_habit(
        self, habit_id: UUID, before_week: date, limit: int = 3
    ) -> list[HabitInstance]:
        items = [
            i
            for i in self._items.values()
            if i.habit_id == habit_id and i.week_start <= before_week
        ]
        items.sort(key=lambda i: i.week_start, reverse=True)
        return items[:limit]

    def seed_instance(
        self, habit_id: UUID, week_start: date, *, done: int, target: int
    ) -> HabitInstance:
        """테스트 보조 — done/target 지정 인스턴스 주입 (S22 페널티 시드)."""
        i = HabitInstance()
        i.id = uuid4()
        i.habit_id = habit_id
        i.week_start = week_start
        i.target_count = target
        i.done_count = done
        self._items[i.id] = i
        self._by_habit_week[(habit_id, week_start)] = i.id
        return i

    async def get_for_week(self, habit_id: UUID, week_start: date) -> HabitInstance | None:
        iid = self._by_habit_week.get((habit_id, week_start))
        return self._items.get(iid) if iid is not None else None

    async def create_or_get_for_week(
        self, habit_id: UUID, week_start: date, target_count: int
    ) -> HabitInstance:
        existing = await self.get_for_week(habit_id, week_start)
        if existing is not None:
            return existing
        i = HabitInstance()
        i.id = uuid4()
        i.habit_id = habit_id
        i.week_start = week_start
        i.target_count = target_count
        i.done_count = 0
        self._items[i.id] = i
        self._by_habit_week[(habit_id, week_start)] = i.id
        return i

    async def increment_done(self, instance: HabitInstance) -> HabitInstance:
        instance.done_count = instance.done_count + 1
        return instance


class FakeInboxRepo:
    """in-memory InboxRepo — Issue #22-B."""

    def __init__(self) -> None:
        self._items: dict[UUID, InboxItem] = {}

    async def list_by_status(self, user_id: UUID, status: str | None = None) -> list[InboxItem]:
        items = [i for i in self._items.values() if i.user_id == user_id and i.archived_at is None]
        if status is not None:
            items = [i for i in items if i.status == status]
        return sorted(items, key=lambda i: i.id, reverse=True)

    async def get_by_id(self, user_id: UUID, inbox_id: UUID) -> InboxItem | None:
        i = self._items.get(inbox_id)
        if i is None or i.user_id != user_id or i.archived_at is not None:
            return None
        return i

    async def create(
        self,
        user_id: UUID,
        raw_text_encrypted: str,
        ai_category_guess: str | None = None,
        status: str = "captured",
    ) -> InboxItem:
        i = InboxItem()
        i.id = uuid4()
        i.user_id = user_id
        i.raw_text_encrypted = raw_text_encrypted
        i.ai_category_guess = ai_category_guess
        i.user_category = None
        i.status = status
        i.promoted_goal_id = None
        i.archived_at = None
        self._items[i.id] = i
        return i

    async def update(
        self,
        item: InboxItem,
        *,
        user_category: str | None = None,
        status: str | None = None,
        ai_category_guess: str | None = None,
    ) -> InboxItem:
        if user_category is not None:
            item.user_category = user_category
        if status is not None:
            item.status = status
        if ai_category_guess is not None:
            item.ai_category_guess = ai_category_guess
        return item

    async def mark_promoted_to_goal(self, item: InboxItem, goal_id: UUID) -> InboxItem:
        item.status = "promoted"
        item.promoted_goal_id = goal_id
        return item

    async def mark_promoted_to_action(self, item: InboxItem) -> InboxItem:
        item.status = "promoted"
        return item

    async def soft_delete(self, item: InboxItem) -> None:
        item.archived_at = datetime.now(UTC)
        item.status = "archived"


class FakeActionItemRepo:
    """in-memory ActionItemRepo — Issue #22-B(create) + #19-A(read by date/id)."""

    def __init__(self) -> None:
        self._items: dict[UUID, ActionItem] = {}

    async def list_by_date(self, user_id: UUID, target_date: date) -> list[ActionItem]:
        items = [
            a
            for a in self._items.values()
            if a.user_id == user_id and a.target_date == target_date and a.archived_at is None
        ]
        return sorted(items, key=lambda a: a.priority)

    async def get_by_id(self, user_id: UUID, action_id: UUID) -> ActionItem | None:
        a = self._items.get(action_id)
        if a is None or a.user_id != user_id or a.archived_at is not None:
            return None
        return a

    async def create_from_inbox(
        self,
        user_id: UUID,
        inbox_item_id: UUID,
        title: str,
        category: str,
        target_date: date,
    ) -> ActionItem:
        a = ActionItem()
        a.id = uuid4()
        a.user_id = user_id
        a.title = title
        a.target_date = target_date
        a.category = category
        a.source = "inbox"
        a.inbox_item_id = inbox_item_id
        a.status = "planned"
        a.priority = 3
        a.estimated_minutes = 30
        a.why_now = None
        a.first_step = None
        a.goal_id = None
        a.archived_at = None
        self._items[a.id] = a
        return a

    async def create_from_recovery(
        self,
        *,
        user_id: UUID,
        parent_action_item_id: UUID,
        title: str,
        category: str,
        source: str,
        target_date: date,
        estimated_minutes: int,
    ) -> ActionItem:
        a = ActionItem()
        a.id = uuid4()
        a.user_id = user_id
        a.title = title
        a.target_date = target_date
        a.category = category
        a.source = source
        a.parent_action_item_id = parent_action_item_id
        a.inbox_item_id = None
        a.status = "planned"
        a.priority = 3
        a.estimated_minutes = estimated_minutes
        a.why_now = None
        a.first_step = None
        a.goal_id = None
        a.archived_at = None
        self._items[a.id] = a
        return a

    def seed(self, action: ActionItem) -> None:
        """테스트 보조 — 카드 직접 주입 (First Plan/manual 카드 시뮬레이션)."""
        self._items[action.id] = action


def _make_strategy(
    code: str,
    group: str,
    label: str,
    template: str,
    min_unit: int,
    primary_tags: list[str],
    allow_rest: bool,
    priority: int,
) -> RecoveryStrategyCatalog:
    s = RecoveryStrategyCatalog()
    s.strategy_type = code
    s.option_group = group
    s.label_ko = label
    s.if_then_template = template
    s.min_recovery_unit_minutes = min_unit
    s.primary_trigger_tags = primary_tags
    s.allow_rest_mode = allow_rest
    s.display_priority = priority
    s.is_active = True
    return s


# 마이그레이션 d09c105520b5 의 9전략 시드 미러 (Issue #20-A)
def default_recovery_strategies() -> list[RecoveryStrategyCatalog]:
    return [
        _make_strategy(
            "NANO_STEP",
            "DOWNSCOPE",
            "5분 단위로 쪼개기",
            "딱 5분만, 첫 단계만 해볼까요? {first_step}",
            5,
            ["AMBIGUITY", "HARD_TO_START"],
            False,
            10,
        ),
        _make_strategy(
            "DOWNSCOPE_DEFAULT",
            "DOWNSCOPE",
            "범위 줄여서 진행",
            "오늘은 절반만, 가능한 만큼만 해볼까요?",
            15,
            ["FATIGUE", "PLAN_TOO_BIG"],
            False,
            20,
        ),
        _make_strategy(
            "ENVIRONMENT_SHIFT",
            "DOWNSCOPE",
            "공간 옮겨서 30분",
            "공간을 옮겨서 30분만 해볼까요? 잘 되는 자리가 있으셨죠.",
            30,
            ["DISTRACTION"],
            False,
            30,
        ),
        _make_strategy(
            "CONTEXT_REWARMING",
            "DOWNSCOPE",
            "맥락 워밍업 5분",
            "{suspended_step} 부터, 5분 워밍업으로 다시 잡아볼까요?",
            5,
            ["CONTEXT_LOSS"],
            False,
            40,
        ),
        _make_strategy(
            "RESCHEDULE_DEFAULT",
            "RESCHEDULE",
            "내일로 옮기기",
            "내일 잘 되는 시간대로 옮겨드릴까요?",
            30,
            ["CONFLICT"],
            False,
            50,
        ),
        _make_strategy(
            "ACTIVE_RECOVERY",
            "RESCHEDULE",
            "산책 후 가볍게",
            "잠깐 산책 20분 후, 가벼운 정리만 해볼까요?",
            20,
            ["LOW_ENERGY", "FATIGUE"],
            True,
            60,
        ),
        _make_strategy(
            "CARRYOVER_DEFAULT",
            "CARRY_OVER",
            "내일 같은 시간",
            "내일 같은 슬롯으로 그대로 옮겨드릴까요?",
            30,
            ["PRIORITY_SHIFT"],
            False,
            70,
        ),
        _make_strategy(
            "FREEZE_SLOT",
            "CARRY_OVER",
            "슬롯 예약 (다음 주)",
            "이번 슬롯은 비워두고 다음 주 같은 시간에 예약할게요.",
            30,
            ["EMERGENCY"],
            False,
            80,
        ),
        _make_strategy(
            "PARK_DEFAULT",
            "PARK",
            "이번 주는 보류",
            "이번 주는 보류하고, 다음 주 리뷰 때 다시 보는 건 어때요?",
            0,
            [],
            True,
            90,
        ),
    ]


class FakeRecoveryRepo:
    """in-memory RecoveryRepo — Issue #20-A. 카탈로그는 마이그레이션 시드 미러."""

    def __init__(
        self,
        *,
        executions: dict[UUID, ExecutionEvent] | None = None,
        failure_tags: dict[UUID, list[str]] | None = None,
    ) -> None:
        # FakeExecutionRepo(#19-B)와 스토어 공유 가능 — E2E 루프 테스트용
        self._executions: dict[UUID, ExecutionEvent] = executions if executions is not None else {}
        self._failure_tags: dict[UUID, list[str]] = failure_tags if failure_tags is not None else {}
        self._attempts: dict[UUID, RecoveryAttempt] = {}
        self._strategies: list[RecoveryStrategyCatalog] = default_recovery_strategies()

    # ── 테스트 보조 seed ──
    def register_execution(
        self,
        *,
        user_id: UUID,
        action_item_id: UUID,
        completion_status: str = "failed",
        failure_tags: list[str] | None = None,
    ) -> ExecutionEvent:
        e = ExecutionEvent()
        e.id = uuid4()
        e.user_id = user_id
        e.action_item_id = action_item_id
        e.scheduled_block_id = uuid4()
        e.plan_start_at = datetime.now(UTC)
        e.plan_end_at = datetime.now(UTC)
        e.completion_status = completion_status
        self._executions[e.id] = e
        self._failure_tags[e.id] = list(failure_tags or [])
        return e

    # ── RecoveryRepo 인터페이스 ──
    async def get_execution(self, user_id: UUID, execution_id: UUID) -> ExecutionEvent | None:
        e = self._executions.get(execution_id)
        if e is None or e.user_id != user_id:
            return None
        return e

    async def list_failure_tag_codes(self, execution_id: UUID) -> list[str]:
        return list(self._failure_tags.get(execution_id, []))

    async def list_active_strategies(self) -> list[RecoveryStrategyCatalog]:
        return sorted(
            (s for s in self._strategies if s.is_active),
            key=lambda s: s.display_priority,
        )

    async def list_attempts(self, user_id: UUID, execution_id: UUID) -> list[RecoveryAttempt]:
        return [
            a
            for a in self._attempts.values()
            if a.user_id == user_id and a.execution_id == execution_id
        ]

    async def get_attempt(self, user_id: UUID, attempt_id: UUID) -> RecoveryAttempt | None:
        a = self._attempts.get(attempt_id)
        if a is None or a.user_id != user_id:
            return None
        return a

    async def create_attempt(
        self,
        *,
        user_id: UUID,
        execution_id: UUID,
        option_group: str,
        strategy_type: str,
        suggested_action_text: str,
        trigger_tag: str | None,
        llm_fallback_used: bool,
    ) -> RecoveryAttempt:
        a = RecoveryAttempt()
        a.id = uuid4()
        a.user_id = user_id
        a.execution_id = execution_id
        a.recovery_option_group = option_group
        a.recovery_strategy_type = strategy_type
        a.suggested_action_text = suggested_action_text
        a.trigger_tag = trigger_tag
        a.llm_fallback_used = llm_fallback_used
        a.user_decision = "pending"
        a.decision_reason = None
        a.recovery_decided_at = None
        a.recovery_started_at = None
        a.recovery_completed_at = None
        a.recovery_duration_minutes = None
        a.recovery_result = "pending"
        a.resulting_action_item_id = None
        a.created_at = datetime.now(UTC)
        self._attempts[a.id] = a
        return a


# 마이그레이션 d09c105520b5 의 13종 실패 사유 미러 (Issue #19-B)
_FAILURE_TAG_SEED: list[tuple[str, str, int]] = [
    ("TIME_SHORTAGE", "시간이 부족했어요", 10),
    ("LOW_ENERGY", "에너지가 낮았어요", 20),
    ("HARD_TO_START", "시작이 어려웠어요", 30),
    ("PRIORITY_SHIFT", "더 중요한 일이 생겼어요", 40),
    ("PLAN_TOO_BIG", "계획이 너무 컸어요", 50),
    ("FATIGUE", "피곤했어요", 60),
    ("AMBIGUITY", "뭘 해야 할지 모호했어요", 70),
    ("CONFLICT", "다른 일정과 겹쳤어요", 80),
    ("OVERRUN", "이전 일이 길어졌어요", 90),
    ("AVOIDANCE", "회피하고 싶었어요", 100),
    ("DISTRACTION", "방해를 받았어요", 110),
    ("EMERGENCY", "급한 일이 있었어요", 120),
    ("CONTEXT_LOSS", "맥락을 잃었어요", 130),
]


def default_failure_tags() -> list[FailureReasonTag]:
    tags: list[FailureReasonTag] = []
    for code, label, order in _FAILURE_TAG_SEED:
        t = FailureReasonTag()
        t.tag_code = code
        t.label_ko = label
        t.description = None
        t.sort_order = order
        t.is_active = True
        tags.append(t)
    return tags


class FakeExecutionRepo:
    """in-memory ExecutionRepo — Issue #19-B.

    `_executions`/`_failure_tags` 는 FakeRecoveryRepo 와 공유 (fixture에서 주입) —
    체크인→실패태깅→복구생성 E2E 루프 테스트를 위해.
    """

    def __init__(self) -> None:
        self._executions: dict[UUID, ExecutionEvent] = {}
        self._failure_tags: dict[UUID, list[str]] = {}
        self._blocks: dict[UUID, ScheduledBlock] = {}
        self._tag_master: list[FailureReasonTag] = default_failure_tags()

    async def get_by_id(self, user_id: UUID, execution_id: UUID) -> ExecutionEvent | None:
        e = self._executions.get(execution_id)
        if e is None or e.user_id != user_id:
            return None
        return e

    async def get_active_for_action(
        self, user_id: UUID, action_item_id: UUID
    ) -> ExecutionEvent | None:
        for e in self._executions.values():
            if (
                e.user_id == user_id
                and e.action_item_id == action_item_id
                and e.completion_status == "in_progress"
            ):
                return e
        return None

    async def find_open_block(self, user_id: UUID, action_item_id: UUID) -> ScheduledBlock | None:
        candidates = [
            b
            for b in self._blocks.values()
            if b.user_id == user_id
            and b.action_item_id == action_item_id
            and b.block_status in ("scheduled", "started")
        ]
        return min(candidates, key=lambda b: b.start_at) if candidates else None

    async def create_adhoc_block(
        self, *, user_id: UUID, action_item: ActionItem, start_at: datetime
    ) -> ScheduledBlock:
        from datetime import timedelta

        b = ScheduledBlock()
        b.id = uuid4()
        b.user_id = user_id
        b.action_item_id = action_item.id
        b.start_at = start_at
        b.end_at = start_at + timedelta(minutes=action_item.estimated_minutes)
        b.block_status = "started"
        b.source = "user_edit"
        b.external_calendar_event_id = None
        self._blocks[b.id] = b
        return b

    async def create_execution(
        self,
        *,
        user_id: UUID,
        action_item_id: UUID,
        block: ScheduledBlock,
        started_at: datetime,
    ) -> ExecutionEvent:
        e = ExecutionEvent()
        e.id = uuid4()
        e.user_id = user_id
        e.action_item_id = action_item_id
        e.scheduled_block_id = block.id
        e.plan_start_at = block.start_at
        e.plan_end_at = block.end_at
        e.actual_start_at = started_at
        e.actual_end_at = None
        e.actual_duration_minutes = None
        e.pause_total_minutes = 0
        e.completion_status = "in_progress"
        e.user_rating = None
        e.user_feedback_encrypted = None
        self._executions[e.id] = e
        self._failure_tags.setdefault(e.id, [])
        return e

    async def get_block(self, block_id: UUID) -> ScheduledBlock | None:
        return self._blocks.get(block_id)

    async def list_active_failure_tags(self) -> list[FailureReasonTag]:
        return sorted((t for t in self._tag_master if t.is_active), key=lambda t: t.sort_order)

    async def has_failure_tags(self, execution_id: UUID) -> bool:
        return len(self._failure_tags.get(execution_id, [])) > 0

    async def add_failure_tags(
        self,
        *,
        execution_id: UUID,
        tag_codes: list[str],
        memo_encrypted: str | None,
    ) -> list[Any]:
        self._failure_tags.setdefault(execution_id, []).extend(tag_codes)
        self._last_memo_encrypted = memo_encrypted
        return []


class FakeDailyBriefRepo:
    """in-memory DailyBriefRepo — Issue #19-A (조회만)."""

    def __init__(self) -> None:
        self._items: dict[tuple[UUID, date], DailyBrief] = {}

    async def get_by_date(self, user_id: UUID, brief_date: date) -> DailyBrief | None:
        return self._items.get((user_id, brief_date))

    async def create(
        self,
        user_id: UUID,
        brief_date: date,
        *,
        headline_text: str,
        expires_at: datetime,
        big_rock_action_item_id: UUID | None = None,
        adjustment_hints: list[dict[str, Any]] | None = None,
        fallback_used: bool = False,
    ) -> DailyBrief:
        b = DailyBrief()
        b.id = uuid4()
        b.user_id = user_id
        b.brief_date = brief_date
        b.headline_text = headline_text
        b.big_rock_action_item_id = big_rock_action_item_id
        b.adjustment_hints = adjustment_hints or []
        b.fallback_used = fallback_used
        b.expires_at = expires_at
        self._items[(user_id, brief_date)] = b
        return b

    def seed(self, brief: DailyBrief) -> None:
        self._items[(brief.user_id, brief.brief_date)] = brief


class FakeReviewRepo:
    """in-memory ReviewRepo — Issue #21-A.

    실행/회복 통계는 테스트가 `seed_execution`/`seed_recovery` 로 주입한다 (집계 입력).
    `upsert_weekly` 는 ORM 없이 PeriodSummary 인스턴스를 만들어 저장한다.
    """

    def __init__(self) -> None:
        self._summaries: dict[tuple[UUID, date], PeriodSummary] = {}
        self._exec_stats: list[ExecutionStat] = []
        self._recovery_stats: list[RecoveryStat] = []

    # ── 테스트 보조 seed ──
    def seed_execution(self, stat: ExecutionStat) -> None:
        self._exec_stats.append(stat)

    def seed_recovery(self, stat: RecoveryStat) -> None:
        self._recovery_stats.append(stat)

    # ── ReviewRepo 인터페이스 ──
    async def get_weekly(self, user_id: UUID, week_start: date) -> PeriodSummary | None:
        return self._summaries.get((user_id, week_start))

    async def collect_execution_stats(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[ExecutionStat]:
        return [s for s in self._exec_stats if start_dt <= s.plan_start_at < end_dt]

    async def collect_recovery_stats(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[RecoveryStat]:
        return list(self._recovery_stats)

    async def upsert_weekly(
        self,
        *,
        user_id: UUID,
        week_start: date,
        week_end: date,
        kpi: Any,
        generated_at: datetime,
    ) -> PeriodSummary:
        ps = self._summaries.get((user_id, week_start)) or PeriodSummary()
        ps.user_id = user_id
        ps.period_type = "weekly"
        ps.start_date = week_start
        ps.end_date = week_end
        ps.adherence_rate = kpi.adherence_rate
        ps.consistency_days = kpi.consistency_days
        ps.resilience_rate = kpi.resilience_rate
        ps.avg_delay_minutes = kpi.avg_delay_minutes
        ps.restart_success_rate = kpi.restart_success_rate
        ps.repeated_failure_count = kpi.repeated_failure_count
        ps.average_recovery_minutes = kpi.average_recovery_minutes
        ps.category_success_rate = kpi.category_success_rate
        ps.peak_point_window = kpi.peak_point_window
        ps.drain_point_window = kpi.drain_point_window
        ps.llm_one_liner = kpi.one_liner
        ps.policy_update_candidates = kpi.policy_update_candidates
        ps.generated_at = generated_at
        self._summaries[(user_id, week_start)] = ps
        return ps


class FakeScheduledBlockRepo:
    """in-memory ScheduledBlockRepo — Issue #21-B.

    실제 join 대신 seed 시 (title, category) 를 함께 보관한다.
    """

    def __init__(self) -> None:
        self._blocks: dict[UUID, ScheduledBlock] = {}
        self._meta: dict[UUID, tuple[str, str]] = {}

    def seed(self, block: ScheduledBlock, *, title: str, category: str) -> None:
        self._blocks[block.id] = block
        self._meta[block.id] = (title, category)

    async def list_week(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[tuple[ScheduledBlock, str, str]]:
        rows = [
            (b, *self._meta[b.id])
            for b in self._blocks.values()
            if b.user_id == user_id and start_dt <= b.start_at < end_dt
        ]
        return sorted(rows, key=lambda r: r[0].start_at)

    async def get_block(self, user_id: UUID, block_id: UUID) -> ScheduledBlock | None:
        b = self._blocks.get(block_id)
        if b is None or b.user_id != user_id:
            return None
        return b

    async def list_overlapping(
        self,
        user_id: UUID,
        start_dt: datetime,
        end_dt: datetime,
        *,
        exclude_block_id: UUID,
    ) -> list[ScheduledBlock]:
        return [
            b
            for b in self._blocks.values()
            if b.user_id == user_id
            and b.id != exclude_block_id
            and b.block_status != "cancelled"
            and b.start_at < end_dt
            and b.end_at > start_dt
        ]


class FakeInterviewRepo:
    """in-memory InterviewRepo — #6 배선. 세션 + 슬롯답 정규화 저장 미러."""

    def __init__(self) -> None:
        self._sessions: dict[UUID, InterviewSessionModel] = {}
        self._answers: dict[UUID, dict[str, InterviewSlotAnswer]] = {}

    async def create_session(self, user_id: UUID, llm_model: str) -> InterviewSessionModel:
        s = InterviewSessionModel()
        s.id = uuid4()
        s.user_id = user_id
        s.llm_model = llm_model
        s.total_turns = 0
        s.ambiguity_final = None
        s.end_reason = None
        s.ended_at = None
        self._sessions[s.id] = s
        self._answers[s.id] = {}
        return s

    async def get_active_session(self, user_id: UUID) -> InterviewSessionModel | None:
        for s in self._sessions.values():
            if s.user_id == user_id and s.end_reason is None:
                return s
        return None

    async def get_active(self, user_id: UUID, session_id: UUID) -> InterviewSessionModel | None:
        s = self._sessions.get(session_id)
        if s is None or s.user_id != user_id:
            return None
        return s

    async def list_slot_answers(self, session_id: UUID) -> list[InterviewSlotAnswer]:
        return list(self._answers.get(session_id, {}).values())

    async def upsert_slot_answer(
        self,
        session_id: UUID,
        slot_key: str,
        value: dict[str, Any] | None,
        *,
        is_required: bool,
        clarity_score: float | None = None,
    ) -> None:
        bucket = self._answers.setdefault(session_id, {})
        existing = bucket.get(slot_key)
        if existing is None:
            a = InterviewSlotAnswer()
            a.id = uuid4()
            a.session_id = session_id
            a.slot_key = slot_key
            a.value = value
            a.clarity_score = clarity_score
            a.is_required = is_required
            bucket[slot_key] = a
        else:
            existing.value = value
            if clarity_score is not None:
                existing.clarity_score = clarity_score

    async def save_progress(
        self, session: InterviewSessionModel, *, total_turns: int, ambiguity_final: float
    ) -> None:
        session.total_turns = total_turns
        session.ambiguity_final = ambiguity_final

    async def finalize(
        self,
        session: InterviewSessionModel,
        *,
        end_reason: str,
        total_turns: int,
        ambiguity_final: float,
    ) -> None:
        session.end_reason = end_reason
        session.total_turns = total_turns
        session.ambiguity_final = ambiguity_final
        session.ended_at = datetime.now(UTC)


class FakePlanDraftRepo:
    """in-memory PlanDraftRepo — #62 First Plan Draft 영속화 미러."""

    def __init__(self) -> None:
        self._items: dict[UUID, PlanDraft] = {}

    async def create(
        self,
        user_id: UUID,
        *,
        target_date: date,
        horizon: str | None,
        ai_source: str,
        payload: dict[str, Any],
        expires_at: datetime,
    ) -> PlanDraft:
        d = PlanDraft()
        d.id = uuid4()
        d.user_id = user_id
        d.status = "draft"
        d.target_date = target_date
        d.horizon = horizon
        d.ai_source = ai_source
        d.payload = payload
        d.expires_at = expires_at
        d.approved_at = None
        d.created_at = datetime.now(UTC)
        d.updated_at = datetime.now(UTC)
        self._items[d.id] = d
        return d

    async def get_by_id(self, user_id: UUID, draft_id: UUID) -> PlanDraft | None:
        d = self._items.get(draft_id)
        if d is None or d.user_id != user_id:
            return None
        return d

    async def mark_approved(self, draft: PlanDraft, *, approved_at: datetime) -> PlanDraft:
        draft.status = "approved"
        draft.approved_at = approved_at
        return draft

    async def expire_stale(self, *, now: datetime) -> int:
        count = 0
        for d in self._items.values():
            if d.status == "draft" and d.expires_at < now:
                d.status = "expired"
                count += 1
        return count


class FakeConsentRepo:
    """in-memory ConsentRepo — Issue #23-B (append-only)."""

    def __init__(self) -> None:
        self._rows: list[UserConsent] = []

    async def list_current(self, user_id: UUID) -> list[UserConsent]:
        seen: set[str] = set()
        latest: list[UserConsent] = []
        for row in reversed(self._rows):  # 최신 추가분 우선
            if row.user_id == user_id and row.consent_type not in seen:
                seen.add(row.consent_type)
                latest.append(row)
        return latest

    async def add(self, user_id: UUID, consent_type: str, *, is_granted: bool) -> UserConsent:
        c = UserConsent()
        c.id = uuid4()
        c.user_id = user_id
        c.consent_type = consent_type
        c.is_granted = is_granted
        c.created_at = datetime.now(UTC)
        self._rows.append(c)
        return c


class FakePrivacyRepo:
    """in-memory PrivacyRepo — Issue #23-B. 실제 마스킹 대신 호출 기록 + 고정 카운트."""

    def __init__(self) -> None:
        self.anonymized_user: UUID | None = None

    async def anonymize_user(self, user_id: UUID) -> int:
        self.anonymized_user = user_id
        return 3


class FakeUserRepo:
    """in-memory UserRepo. /auth 흐름 + 상태 전이 헬퍼 둘 다 지원."""

    def __init__(self) -> None:
        self._by_email: dict[str, User] = {}
        self._by_id: dict[UUID, User] = {}

    def register(self, user: User) -> None:
        """테스트가 미리 user 를 등록할 때 사용 (`client` fixture 가 demo user 자동 등록)."""
        self._by_email[user.email] = user
        self._by_id[user.id] = user

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self._by_id.get(user_id)

    async def get_by_email(self, email: str) -> User | None:
        return self._by_email.get(email)

    async def upsert_from_google(self, profile: GoogleProfile) -> User:
        existing = self._by_email.get(profile.email)
        if existing is not None:
            existing.name = profile.name
            return existing
        u = User()
        u.id = uuid4()
        u.email = profile.email
        u.name = profile.name
        u.timezone = "Asia/Seoul"
        u.onboarding_state = "WELCOME"
        u.tone_mode = None
        self._by_email[profile.email] = u
        self._by_id[u.id] = u
        return u

    async def set_tone_mode(self, user: User, tone_mode: str) -> User:
        user.tone_mode = tone_mode
        return user

    async def advance_onboarding(
        self,
        user: User,
        expected_from: str | tuple[str, ...],
        to: str,
    ) -> bool:
        expected = (expected_from,) if isinstance(expected_from, str) else expected_from
        if user.onboarding_state in expected:
            user.onboarding_state = to
            return True
        return False


# ───── 일반 도메인 client (인증 + 모든 fake) ─────


@pytest.fixture
def demo_user_orm() -> User:
    """demo user ORM 인스턴스 — 테스트가 `onboarding_state` 직접 변경 가능."""
    return make_demo_user()


@pytest.fixture
def fake_time_policy_repo() -> FakeTimePolicyRepo:
    return FakeTimePolicyRepo()


@pytest.fixture
def fake_fixed_schedule_repo() -> FakeFixedScheduleRepo:
    return FakeFixedScheduleRepo()


@pytest.fixture
def fake_notification_repo() -> FakeNotificationRepo:
    return FakeNotificationRepo()


@pytest.fixture
def fake_user_repo() -> FakeUserRepo:
    return FakeUserRepo()


@pytest.fixture
def fake_consent_repo() -> FakeConsentRepo:
    return FakeConsentRepo()


@pytest.fixture
def fake_privacy_repo() -> FakePrivacyRepo:
    return FakePrivacyRepo()


@pytest.fixture
def fake_goal_repo() -> FakeGoalRepo:
    return FakeGoalRepo()


@pytest.fixture
def fake_habit_repo() -> FakeHabitRepo:
    return FakeHabitRepo()


@pytest.fixture
def fake_habit_instance_repo() -> FakeHabitInstanceRepo:
    return FakeHabitInstanceRepo()


@pytest.fixture
def fake_inbox_repo() -> FakeInboxRepo:
    return FakeInboxRepo()


@pytest.fixture
def fake_action_item_repo() -> FakeActionItemRepo:
    return FakeActionItemRepo()


@pytest.fixture
def fake_interview_repo() -> FakeInterviewRepo:
    return FakeInterviewRepo()


@pytest.fixture
def fake_execution_repo() -> FakeExecutionRepo:
    return FakeExecutionRepo()


@pytest.fixture
def fake_recovery_repo(fake_execution_repo: FakeExecutionRepo) -> FakeRecoveryRepo:
    # 실행/실패태그 스토어를 ExecutionRepo 와 공유 — 체크인→복구 E2E 가능
    return FakeRecoveryRepo(
        executions=fake_execution_repo._executions,
        failure_tags=fake_execution_repo._failure_tags,
    )


@pytest.fixture
def fake_daily_brief_repo() -> FakeDailyBriefRepo:
    return FakeDailyBriefRepo()


@pytest.fixture
def fake_review_repo() -> FakeReviewRepo:
    return FakeReviewRepo()


@pytest.fixture
def fake_scheduled_block_repo() -> FakeScheduledBlockRepo:
    return FakeScheduledBlockRepo()


@pytest.fixture
def fake_plan_draft_repo() -> FakePlanDraftRepo:
    return FakePlanDraftRepo()


@pytest.fixture
def client(
    demo_user_orm: User,
    fake_time_policy_repo: FakeTimePolicyRepo,
    fake_fixed_schedule_repo: FakeFixedScheduleRepo,
    fake_notification_repo: FakeNotificationRepo,
    fake_user_repo: FakeUserRepo,
    fake_goal_repo: FakeGoalRepo,
    fake_habit_repo: FakeHabitRepo,
    fake_habit_instance_repo: FakeHabitInstanceRepo,
    fake_inbox_repo: FakeInboxRepo,
    fake_action_item_repo: FakeActionItemRepo,
    fake_interview_repo: FakeInterviewRepo,
    fake_daily_brief_repo: FakeDailyBriefRepo,
    fake_plan_draft_repo: FakePlanDraftRepo,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_execution_repo: FakeExecutionRepo,
    fake_consent_repo: FakeConsentRepo,
    fake_privacy_repo: FakePrivacyRepo,
    fake_review_repo: FakeReviewRepo,
    fake_scheduled_block_repo: FakeScheduledBlockRepo,
) -> Iterator[TestClient]:
    """기본 client — 인증된 demo user + 도메인 fake repo + fake session."""
    _reset_process_singletons()
    fake_user_repo.register(demo_user_orm)
    app = create_app()

    async def _fake_session_gen() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    app.dependency_overrides[get_current_user] = lambda: demo_user_orm
    app.dependency_overrides[get_db] = _fake_session_gen
    app.dependency_overrides[get_time_policy_repo] = lambda: fake_time_policy_repo
    app.dependency_overrides[get_fixed_schedule_repo] = lambda: fake_fixed_schedule_repo
    app.dependency_overrides[get_notification_repo] = lambda: fake_notification_repo
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_goal_repo] = lambda: fake_goal_repo
    app.dependency_overrides[get_habit_repo] = lambda: fake_habit_repo
    app.dependency_overrides[get_habit_instance_repo] = lambda: fake_habit_instance_repo
    app.dependency_overrides[get_inbox_repo] = lambda: fake_inbox_repo
    app.dependency_overrides[get_action_item_repo] = lambda: fake_action_item_repo
    app.dependency_overrides[get_interview_repo] = lambda: fake_interview_repo
    app.dependency_overrides[get_daily_brief_repo] = lambda: fake_daily_brief_repo
    app.dependency_overrides[get_plan_draft_repo] = lambda: fake_plan_draft_repo
    app.dependency_overrides[get_recovery_repo] = lambda: fake_recovery_repo
    app.dependency_overrides[get_execution_repo] = lambda: fake_execution_repo
    app.dependency_overrides[get_consent_repo] = lambda: fake_consent_repo
    app.dependency_overrides[get_privacy_repo] = lambda: fake_privacy_repo
    app.dependency_overrides[get_review_repo] = lambda: fake_review_repo
    app.dependency_overrides[get_scheduled_block_repo] = lambda: fake_scheduled_block_repo
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def unauthed_client() -> Iterator[TestClient]:
    """override 없는 fresh client + fake session — 401 분기 / Authorization 헤더 테스트용."""
    _reset_process_singletons()
    app = create_app()

    async def _fake_session_gen() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    app.dependency_overrides[get_db] = _fake_session_gen
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_client(fake_user_repo: FakeUserRepo) -> Iterator[TestClient]:
    """`/auth/*` 테스트 — repo/session 만 override, 인증은 실제 JWT 흐름."""
    _reset_process_singletons()
    app = create_app()

    async def _fake_session_gen() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    app.dependency_overrides[get_db] = _fake_session_gen
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    with TestClient(app) as c:
        yield c


def issue_helper_token(
    *,
    user_id: UUID,
    token_type: str,
    expired: bool = False,
) -> str:
    """테스트 보조 — JWT 직접 발급 (만료 강제 포함)."""
    from datetime import timedelta

    import jwt as pyjwt

    from reaction_backend.config import get_settings

    cfg = get_settings()
    now = datetime.now(UTC)
    if expired:
        iat = now - timedelta(hours=2)
        exp = now - timedelta(hours=1)
    else:
        iat = now
        exp = now + timedelta(hours=1)
    payload = {
        "sub": str(user_id),
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
        "type": token_type,
        "jti": "test-jti",
    }
    return pyjwt.encode(payload, cfg.jwt_secret, algorithm=cfg.jwt_algorithm)


__all__ = [
    "DEMO_USER_UUID",
    "FakeActionItemRepo",
    "FakeConsentRepo",
    "FakeDailyBriefRepo",
    "FakeExecutionRepo",
    "FakeFixedScheduleRepo",
    "FakeGoalRepo",
    "FakeHabitInstanceRepo",
    "FakeHabitRepo",
    "FakeInboxRepo",
    "FakeNotificationRepo",
    "FakePlanDraftRepo",
    "FakePrivacyRepo",
    "FakeRecoveryRepo",
    "FakeReviewRepo",
    "FakeScheduledBlockRepo",
    "FakeTimePolicyRepo",
    "FakeUserRepo",
    "auth_client",
    "client",
    "demo_user_orm",
    "fake_action_item_repo",
    "fake_consent_repo",
    "fake_daily_brief_repo",
    "fake_execution_repo",
    "fake_fixed_schedule_repo",
    "fake_goal_repo",
    "fake_habit_instance_repo",
    "fake_habit_repo",
    "fake_inbox_repo",
    "fake_notification_repo",
    "fake_plan_draft_repo",
    "fake_privacy_repo",
    "fake_recovery_repo",
    "fake_review_repo",
    "fake_scheduled_block_repo",
    "fake_time_policy_repo",
    "fake_user_repo",
    "issue_helper_token",
    "make_demo_user",
    "unauthed_client",
]
