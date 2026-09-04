"""공통 pytest fixture.

Issue #16 이후 모든 도메인 라우터(health 제외)에 `Depends(get_current_user)` 적용.
Issue #17 이후 4 도메인(time_policies / fixed_schedules / notifications) 실 DB 의존.

테스트 격리를 위해:
- `client`         : 인증 override + 4 도메인 fake repo + fake session. 일반 도메인 테스트용.
- `unauthed_client`: 인증 override 없음 (DB 의존성만 fake) — 401 분기 검증.
- `auth_client`    : 인증 override 없음 + user_repo 만 fake — `/auth/*` 흐름 (실 JWT 발급).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, date, datetime, time
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import get_current_user
from reaction_backend.auth.revoke import get_revoke_store
from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.behavioral_profile import BehavioralProfile
from reaction_backend.db.models.daily_brief import DailyBrief
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.failure_reason_tag import FailureReasonTag
from reaction_backend.db.models.fixed_schedule import FixedSchedule
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.habit import Habit
from reaction_backend.db.models.habit_instance import HabitInstance
from reaction_backend.db.models.inbox_item import InboxItem
from reaction_backend.db.models.interaction_style import InteractionStyle
from reaction_backend.db.models.interruption_event import InterruptionEvent
from reaction_backend.db.models.interview_session import InterviewSession as InterviewSessionModel
from reaction_backend.db.models.interview_slot_answer import InterviewSlotAnswer
from reaction_backend.db.models.invite_code import InviteCode
from reaction_backend.db.models.notification_send import NotificationSend
from reaction_backend.db.models.notification_setting import NotificationSetting
from reaction_backend.db.models.period_summary import PeriodSummary
from reaction_backend.db.models.plan_draft import PlanDraft
from reaction_backend.db.models.policy_snapshot import PolicySnapshot
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
from reaction_backend.repositories.invite_code_repo import get_invite_code_repo, normalize_code
from reaction_backend.repositories.notification_repo import get_notification_repo
from reaction_backend.repositories.notification_send_repo import get_notification_send_repo
from reaction_backend.repositories.plan_draft_repo import get_plan_draft_repo
from reaction_backend.repositories.policy_snapshot_repo import get_policy_snapshot_repo
from reaction_backend.repositories.privacy_repo import get_privacy_repo
from reaction_backend.repositories.profile_repo import get_profile_repo
from reaction_backend.repositories.recovery_repo import get_recovery_repo
from reaction_backend.repositories.review_repo import TopFailureContext, get_review_repo
from reaction_backend.repositories.scheduled_block_repo import get_scheduled_block_repo
from reaction_backend.repositories.time_policy_repo import get_time_policy_repo
from reaction_backend.repositories.user_repo import GoogleProfile, get_user_repo
from reaction_backend.schemas.common import KST, now_kst

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
    # 로컬 .env 에 GEMINI_API_KEY 가 있어도 테스트 시에는 비워서 fallback 을 타게 격리
    monkeypatch.setenv("GEMINI_API_KEY", "")

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


class _FakeNestedTx:
    """`_FakeSession.begin_nested()` 용 no-op savepoint (프로필 영속 best-effort 가드 #130)."""

    async def __aenter__(self) -> _FakeNestedTx:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False  # 예외 억제 안 함 — 호출부 try/except 가 처리


class _FakeSession:
    """라우터가 호출하는 session 인터페이스의 stub.

    repo 들이 모두 dependency_override 로 fake 로 교체되므로 session 은 직접 query 수행 X.
    `time_policies` prefill 의 inline select 만 fake — 항상 빈 결과 (interview 답 없음).

    `lock_acquired` 는 advisory lock(ADR-0005 §7.6) 의 `pg_try_advisory_lock` 결과를 흉내낸다
    (default True = 획득 성공). False 로 두면 동시 진입(409) 분기를 테스트할 수 있다.
    """

    def __init__(self, *, lock_acquired: bool = True) -> None:
        self.lock_acquired = lock_acquired
        # commit/rollback 이 계약인 코드(알림 sweep 의 건당 commit 등)를 검증할 카운터 —
        # no-op fake 는 "commit 을 지워도 전 스위트 초록" 뮤테이션을 못 잡는다 (#20 리뷰).
        self.commit_count = 0
        self.rollback_count = 0

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1

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

    def begin_nested(self) -> _FakeNestedTx:
        # profile persist 가 savepoint 로 감싸므로 fake 도 no-op CM 제공 (#130).
        return _FakeNestedTx()


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

    async def set_push_subscription(
        self, setting: NotificationSetting, subscription: dict[str, Any]
    ) -> NotificationSetting:
        setting.push_subscription = subscription
        return setting

    async def clear_push_subscription(self, setting: NotificationSetting) -> NotificationSetting:
        setting.push_subscription = None
        return setting


class FakeNotificationSendRepo:
    """in-memory NotificationSendRepo — 발송 게이트(push_gate) 테스트용 (#20).

    실 repo 의 WHERE/락 SQL 은 fake 로는 절대 실행되지 않는다 —
    `tests/test_notification_send_repo_sql.py` 가 실 SQL 문자열로 별도 고정.
    `ops` 는 게이트가 락을 **이력 조회보다 먼저** 잡는지(TOCTOU 방지 순서) 검증용.
    """

    def __init__(self) -> None:
        self._sends: list[NotificationSend] = []
        self.ops: list[str] = []

    async def lock_user(self, user_id: UUID) -> None:  # noqa: ARG002 — 실 repo 시그니처 유지
        self.ops.append("lock")

    async def count_sent_since(self, user_id: UUID, *, since: datetime) -> int:
        self.ops.append("count")
        return sum(1 for s in self._sends if s.user_id == user_id and s.sent_at >= since)

    async def class_sent_since(
        self, user_id: UUID, *, notification_class: str, since: datetime
    ) -> bool:
        self.ops.append("dedup")
        return any(
            s.user_id == user_id
            and s.notification_class == notification_class
            and s.sent_at >= since
            for s in self._sends
        )

    async def record(
        self,
        *,
        id: UUID,  # noqa: A002 — 실 repo 시그니처 유지
        user_id: UUID,
        notification_class: str,
        sent_at: datetime,
        target_action_item_id: UUID | None = None,
    ) -> NotificationSend:
        self.ops.append("record")
        row = NotificationSend()
        row.id = id
        row.user_id = user_id
        row.notification_class = notification_class
        row.sent_at = sent_at
        row.target_action_item_id = target_action_item_id
        row.opened_at = None
        self._sends.append(row)
        return row

    async def get_by_id(self, notification_id: UUID, user_id: UUID) -> NotificationSend | None:
        return next(
            (s for s in self._sends if s.id == notification_id and s.user_id == user_id), None
        )

    async def stamp_opened(self, notification: NotificationSend, opened_at: datetime) -> None:
        if notification.opened_at is None:
            notification.opened_at = opened_at


@pytest.fixture
def fake_notification_send_repo() -> FakeNotificationSendRepo:
    return FakeNotificationSendRepo()


class FakeWebPushSender:
    """전송 기록 sender — outcome 을 지정해 gone/error/unconfigured 분기를 테스트."""

    def __init__(self, outcome: str = "ok") -> None:
        self.outcome = outcome
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    @property
    def is_configured(self) -> bool:
        return self.outcome != "unconfigured"

    async def send(self, subscription: dict[str, Any], payload: dict[str, Any]) -> str:
        self.calls.append((subscription, payload))
        return self.outcome


class FakeGoalRepo:
    """in-memory GoalRepo — Issue #22."""

    def __init__(self) -> None:
        self._items: dict[UUID, Goal] = {}
        # goal_id → 분해 노드. 실 repo 는 goal_nodes 테이블을 읽는다(계획 승인이 채움).
        self._nodes: dict[UUID, list[Any]] = {}

    async def list_active(self, user_id: UUID) -> list[Goal]:
        return [g for g in self._items.values() if g.user_id == user_id and g.archived_at is None]

    async def list_nodes(self, goal_id: UUID, *, tree_kind: str = "plan") -> list[Any]:
        """분해 트리는 계획 승인이 만든다 — fake 목표 CRUD 만으로는 항상 비어 있다.

        `tree_kind` 필터는 실 repo 와 동일 규약(기본값 "plan") — 시드된 노드가 있으면
        `getattr(n, "tree_kind", "plan")` 로 걸러(구버전 시드 객체도 안전하게 "plan" 취급).
        """
        return [
            n for n in self._nodes.get(goal_id, []) if getattr(n, "tree_kind", "plan") == tree_kind
        ]

    async def get_by_id(self, user_id: UUID, goal_id: UUID) -> Goal | None:
        g = self._items.get(goal_id)
        if g is None or g.user_id != user_id or g.archived_at is not None:
            return None
        return g

    async def get_ultimate(self, user_id: UUID) -> Goal | None:
        for g in self._items.values():
            if g.user_id == user_id and g.is_ultimate and g.archived_at is None:
                return g
        return None

    async def get_mandala_node(self, user_id: UUID, node_id: UUID) -> Any | None:
        """실 repo 와 동일 — goal 소유권 + `tree_kind='mandala'` + 미보관만 통과."""
        for goal_id, nodes in self._nodes.items():
            goal = self._items.get(goal_id)
            if goal is None or goal.user_id != user_id:
                continue
            for n in nodes:
                if (
                    n.id == node_id
                    and getattr(n, "tree_kind", "plan") == "mandala"
                    and n.archived_at is None
                ):
                    return n
        return None

    async def set_completed(self, goal: Goal, *, completed: bool) -> Goal:
        """실 repo 와 동일 — `status` 만 바꾸고 `archived_at` 은 안 건드린다."""
        goal.status = "completed" if completed else "active"
        return goal

    async def get_plan_milestone_node(
        self, user_id: UUID, goal_id: UUID, node_id: UUID
    ) -> Any | None:
        """실 repo 와 동일 — 소유권 + goal 일치 + `tree_kind='plan'` + `node_type='milestone'`
        + 미보관만 통과. **WHERE 절 자체는 여기서 검증되지 않는다** — 실 SQL 은
        `tests/test_milestone_completion_real_db.py` 가 실 Postgres 로 고정한다.
        """
        goal = self._items.get(goal_id)
        if goal is None or goal.user_id != user_id:
            return None
        for n in self._nodes.get(goal_id, []):
            if (
                n.id == node_id
                and getattr(n, "tree_kind", "plan") == "plan"
                and getattr(n, "node_type", None) == "milestone"
                and n.archived_at is None
            ):
                return n
        return None

    async def count_by_tier(self, user_id: UUID, tier: str) -> int:
        # 실 repo 와 동일하게 잠정(proposed)·완료(completed) 목표는 한도에서 제외.
        return sum(
            1
            for g in self._items.values()
            if g.user_id == user_id
            and g.archived_at is None
            and g.goal_tier == tier
            and g.status not in ("proposed", "completed")
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
        g.is_ultimate = False
        g.archived_at = None
        self._items[g.id] = g
        return g

    async def update(
        self,
        goal: Goal,
        *,
        title: str | None = None,
        category: str | None = None,
        deadline: date | None = None,
        priority_level: int | None = None,
        goal_tier: str | None = None,
    ) -> Goal:
        if title is not None:
            goal.title = title
        if category is not None:
            goal.category = category
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

    async def expire_stale_proposed(self, *, before: datetime, archived_at: datetime) -> int:
        # 실 GoalRepo.expire_stale_proposed 의 WHERE 를 손으로 그대로 옮긴다 (#178) —
        # status=='proposed' + archived_at IS NULL + created_at < before, 셋 다 있어야 한다.
        n = 0
        for g in self._items.values():
            if g.status == "proposed" and g.archived_at is None and g.created_at < before:
                g.status = "archived"
                g.archived_at = archived_at
                n += 1
        return n


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

    async def get_active_by_goal_node(self, user_id: UUID, goal_node_id: UUID) -> Habit | None:
        for h in self._items.values():
            if h.user_id == user_id and h.goal_node_id == goal_node_id and h.archived_at is None:
                return h
        return None

    async def create(
        self,
        user_id: UUID,
        title: str,
        category: str,
        frequency_per_week: int,
        minutes_per_session: int,
        time_preference: str,
        priority_level: int,
        goal_node_id: UUID | None = None,
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
        h.goal_node_id = goal_node_id
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

    def __init__(self, habits: FakeHabitRepo | None = None) -> None:
        self._items: dict[UUID, HabitInstance] = {}
        self._by_habit_week: dict[tuple[UUID, date], UUID] = {}
        # 실 repo 는 joinedload(habit) — 오늘 어젠다(_habit_schema)가 title 을 읽는다.
        self._habits = habits

    async def list_for_user_week(self, user_id: UUID, week_start: date) -> list[HabitInstance]:
        items = [i for i in self._items.values() if i.week_start == week_start]
        if self._habits is not None:
            for i in items:
                i.habit = self._habits._items.get(i.habit_id)
        return items

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
        instance.done_count = min(instance.done_count + 1, instance.target_count)
        return instance


class FakeInboxRepo:
    """in-memory InboxRepo — Issue #22-B."""

    def __init__(self) -> None:
        self._items: dict[UUID, InboxItem] = {}

    async def list_by_status(self, user_id: UUID, status: str | None = None) -> list[InboxItem]:
        mine = [i for i in self._items.values() if i.user_id == user_id]
        if status == "archived":
            items = [i for i in mine if i.status == "archived"]
        else:
            items = [i for i in mine if i.archived_at is None]
            if status is not None:
                items = [i for i in items if i.status == status]
        return sorted(items, key=lambda i: i.id, reverse=True)

    async def get_by_id(self, user_id: UUID, inbox_id: UUID) -> InboxItem | None:
        i = self._items.get(inbox_id)
        if i is None or i.user_id != user_id or i.archived_at is not None:
            return None
        return i

    async def get_by_id_any(self, user_id: UUID, inbox_id: UUID) -> InboxItem | None:
        i = self._items.get(inbox_id)
        if i is None or i.user_id != user_id:
            return None
        return i

    async def restore(self, item: InboxItem) -> InboxItem:
        if item.archived_at is None:
            return item
        item.archived_at = None
        item.status = "classified" if item.ai_category_guess is not None else "captured"
        return item

    async def create(
        self,
        user_id: UUID,
        raw_text_encrypted: str,
        ai_category_guess: str | None = None,
        status: str = "captured",
        *,
        source: str = "user",
        resource_slug: str | None = None,
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
        # ORM server_default 는 이 fake 에 적용되지 않는다 — 실 DB 와 같은 값을 명시한다.
        i.source = source
        i.resource_slug = resource_slug
        self._items[i.id] = i
        return i

    async def has_resource(self, user_id: UUID, resource_slug: str) -> bool:
        """실 repo 와 같이 **archived 도 포함**해서 센다 (BE #171)."""
        return any(
            x.user_id == user_id and x.resource_slug == resource_slug for x in self._items.values()
        )

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
        self._block_repo: FakeScheduledBlockRepo | None = None
        #: 잠금 읽기(`get_by_id_for_update`)가 불린 action_id 들 — 배선 핀용 (#368).
        self.locking_reads: list[UUID] = []

    def link_blocks(self, block_repo: FakeScheduledBlockRepo) -> None:
        """활성 블록 유무로 백로그를 걸러내도록 block repo 를 연결(list_planned_without_block)."""
        self._block_repo = block_repo

    async def list_planned_without_block(self, user_id: UUID) -> list[ActionItem]:
        """활성 블록이 하나도 없는 planned 카드 — 미배치 백로그 (실 repo 규칙 미러)."""
        blocked: set[UUID] = set()
        if self._block_repo is not None:
            for b in self._block_repo._blocks.values():
                if b.user_id == user_id and b.block_status != "cancelled":
                    blocked.add(b.action_item_id)
        items = [
            a
            for a in self._items.values()
            if a.user_id == user_id
            and a.archived_at is None
            and a.status == "planned"
            and a.id not in blocked
        ]
        return sorted(items, key=lambda a: a.priority)

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

    async def get_by_id_for_update(self, user_id: UUID, action_id: UUID) -> ActionItem | None:
        """`get_by_id` + 행 잠금 (실 repo 규칙 미러, #368).

        in-memory 라 잠금 자체는 흉내낼 수 없다 — 실제 직렬화는
        `tests/test_start_action_locking.py` 가 실 Postgres 커넥션 두 개로 검증한다.
        여기서는 **어느 경로가 잠금 읽기를 쓰는지**만 기록해, 라우터가 락 없는
        `get_by_id` 로 되돌아가면 배선 테스트가 잡게 한다.
        """
        self.locking_reads.append(action_id)
        return await self.get_by_id(user_id, action_id)

    async def get_by_id_any(self, user_id: UUID, action_id: UUID) -> ActionItem | None:
        """보관분 포함 — 취소 멱등 판정용 (실 repo 규칙 미러, BE #214)."""
        a = self._items.get(action_id)
        if a is None or a.user_id != user_id:
            return None
        return a

    async def cancel(self, action: ActionItem) -> None:
        """`archived_at` 만 세팅 — status 는 건드리지 않는다 (실 repo 규칙 미러)."""
        if action.archived_at is None:
            action.archived_at = datetime.now(UTC)

    async def find_adopted_step(
        self,
        user_id: UUID,
        inbox_item_id: UUID,
        title: str,
        target_date: date,
    ) -> ActionItem | None:
        """같은 걸음의 활성 카드 (실 repo 규칙 미러, #213)."""
        matches = [
            a
            for a in self._items.values()
            if a.user_id == user_id
            and a.inbox_item_id == inbox_item_id
            and a.title == title
            and a.target_date == target_date
            and a.archived_at is None
        ]
        matches.sort(key=lambda a: a.id.hex)  # created_at 대용 — 결정적이기만 하면 된다
        return matches[0] if matches else None

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
        # 실 repo 와 같이 부모에서 물려받는다 (#367) — fake 가 None 으로 두면 "회복 카드는
        # 어느 목표에도 안 걸린다" 는 옛 동작이 테스트 안에서만 계속 살아남는다.
        parent = self._items.get(parent_action_item_id)
        a.goal_id = parent.goal_id if parent is not None else None
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
            "급한 일 먼저, 같은 슬롯 유지",
            "급한 일이 먼저였잖아요. 같은 시간대를 그대로 지켜서 다시 잡아드릴까요?",
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
        # alembic 8680c4567ca6 — 태그 구멍(TIME_SHORTAGE/OVERRUN/AVOIDANCE) + PARK 도달 경로.
        _make_strategy(
            "TIMEBOX_REBUDGET",
            "RESCHEDULE",
            "실측 시간으로 재산정",
            "이 카드는 보통 그보다 시간이 더 걸렸어요. 다음엔 여유를 두고 다시 잡아드릴까요?",
            15,
            ["TIME_SHORTAGE", "OVERRUN"],
            False,
            55,
        ),
        _make_strategy(
            "BUFFER_INSERT",
            "RESCHEDULE",
            "다음 슬롯에 여유 넣기",
            "직전 일이 길어졌던 날이었어요. 다음 슬롯 앞에 15분 여유를 넣어둘까요?",
            15,
            ["OVERRUN"],
            False,
            58,
        ),
        _make_strategy(
            "SELF_FORGIVENESS_NANO",
            "DOWNSCOPE",
            "지난 일은 접어두고 한 걸음만",
            "어제 미룬 건 이미 지난 일로 두어요. 지금은 딱 한 걸음만 떼어볼까요?",
            5,
            ["AVOIDANCE", "HARD_TO_START"],
            False,
            15,
        ),
        _make_strategy(
            "GOAL_RECHECK",
            "PARK",
            "목표 다시 확인하기",
            "이 목표, 지금도 하고 싶은 게 맞을까요? 잠시 접어두고 다음 주 리뷰 때 다시 볼까요?",
            0,
            ["AVOIDANCE", "PRIORITY_SHIFT"],
            True,
            85,
        ),
    ]


class FakeRecoveryRepo:
    """in-memory RecoveryRepo — Issue #20-A. 카탈로그는 마이그레이션 시드 미러."""

    def __init__(
        self,
        *,
        executions: dict[UUID, ExecutionEvent] | None = None,
        failure_tags: dict[UUID, list[str]] | None = None,
        actions: dict[UUID, ActionItem] | None = None,
    ) -> None:
        # FakeExecutionRepo(#19-B)와 스토어 공유 가능 — E2E 루프 테스트용
        self._executions: dict[UUID, ExecutionEvent] = executions if executions is not None else {}
        self._failure_tags: dict[UUID, list[str]] = failure_tags if failure_tags is not None else {}
        self._attempts: dict[UUID, RecoveryAttempt] = {}
        self._strategies: list[RecoveryStrategyCatalog] = default_recovery_strategies()
        # FakeActionItemRepo 와 스토어 공유(fixture 주입) — list_goal_outcomes(L3) 가
        # action_item_id → goal_id 를 알아야 한다.
        self._actions: dict[UUID, ActionItem] = actions if actions is not None else {}

    # ── 테스트 보조 seed ──
    def register_execution(
        self,
        *,
        user_id: UUID,
        action_item_id: UUID,
        completion_status: str = "failed",
        failure_tags: list[str] | None = None,
        plan_start_at: datetime | None = None,
    ) -> ExecutionEvent:
        e = ExecutionEvent()
        e.id = uuid4()
        e.user_id = user_id
        e.action_item_id = action_item_id
        e.scheduled_block_id = uuid4()
        # 기본값(now)은 카드 target_date(2026-06-05 고정)와 어긋나 day_delta 가 수십 일이 된다
        # — 그래서 기본 시드로는 #174(과거 슬롯)가 재현되지 않는다. 재현하려면 명시로 넘길 것.
        e.plan_start_at = plan_start_at or datetime.now(UTC)
        e.plan_end_at = e.plan_start_at
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

    async def list_lineage_outcomes_for_tag(
        self, user_id: UUID, action_item_id: UUID, tag_code: str, *, limit: int = 20
    ) -> list[str]:
        """단순화 — fake 엔 `goal_id` 개념이 없어 "계보"를 action_item_id 자기 자신으로
        근사한다(실 repo 는 같은 goal_id 전체 — `orchestrator/recovery.py` L2 배선 PR 참고).
        실 goal 계보 동작은 `tests/test_recovery_repo_lineage.py`(실 Postgres)가 검증한다.
        """
        rows = [
            e
            for e in self._executions.values()
            if e.user_id == user_id
            and e.action_item_id == action_item_id
            and e.completion_status != "in_progress"
        ]
        rows.sort(key=lambda e: e.plan_start_at, reverse=True)
        outcomes: list[str] = []
        for e in rows[:limit]:
            if e.completion_status == "failed" and tag_code not in self._failure_tags.get(e.id, []):
                outcomes.append("partial_done")
            else:
                outcomes.append(e.completion_status)
        return outcomes

    async def list_same_card_outcomes(
        self, user_id: UUID, action_item_id: UUID, *, limit: int = 20
    ) -> list[str]:
        rows = [
            e
            for e in self._executions.values()
            if e.user_id == user_id
            and e.action_item_id == action_item_id
            and e.completion_status != "in_progress"
        ]
        rows.sort(key=lambda e: e.plan_start_at, reverse=True)
        return [e.completion_status for e in rows[:limit]]

    async def list_recovery_results(self, user_id: UUID, *, limit: int = 20) -> list[str]:
        rows = [
            a
            for a in self._attempts.values()
            if a.user_id == user_id and a.recovery_result != "pending"
        ]
        rows.sort(
            key=lambda a: a.recovery_decided_at or datetime.min.replace(tzinfo=UTC), reverse=True
        )
        return [a.recovery_result for a in rows[:limit]]

    async def list_goal_outcomes(
        self, user_id: UUID, goal_id: UUID, *, limit: int = 20
    ) -> list[str]:
        rows = [
            e
            for e in self._executions.values()
            if e.user_id == user_id
            and e.completion_status != "in_progress"
            and self._actions.get(e.action_item_id) is not None
            and self._actions[e.action_item_id].goal_id == goal_id
        ]
        rows.sort(key=lambda e: e.plan_start_at, reverse=True)
        return [e.completion_status for e in rows[:limit]]

    async def list_recovery_decisions(self, user_id: UUID, *, limit: int = 20) -> list[str]:
        rows = [
            a
            for a in self._attempts.values()
            if a.user_id == user_id and a.user_decision != "pending"
        ]
        rows.sort(
            key=lambda a: a.recovery_decided_at or datetime.min.replace(tzinfo=UTC), reverse=True
        )
        return [a.user_decision for a in rows[:limit]]

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

    async def list_due_re_engagement(
        self, user_id: UUID, target_date: date
    ) -> list[RecoveryAttempt]:
        return [
            a
            for a in self._attempts.values()
            if a.user_id == user_id
            and a.re_engagement_anchor_at is not None
            and a.re_engagement_anchor_at.astimezone(KST).date() == target_date
        ]

    async def get_strategy(self, strategy_type: str) -> RecoveryStrategyCatalog | None:
        # 실 repo 와 같이 **is_active 필터 포함** — 비활성이면 None 이라 호출자가 기본
        # 회복 단위(5분)로 떨어지는 동작이 fake 에서도 재현돼야 한다.
        return next(
            (s for s in self._strategies if s.strategy_type == strategy_type and s.is_active),
            None,
        )

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
        prompt_version: str | None = None,
        obstacle: str | None = None,
        coping_clause: str | None = None,
        acknowledgment: str | None = None,
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
        a.prompt_version = prompt_version
        a.obstacle = obstacle
        a.coping_clause = coping_clause
        a.acknowledgment = acknowledgment
        a.assigned_arm = None
        a.first_viewed_at = None
        a.user_decision = "pending"
        a.decision_reason = None
        a.recovery_decided_at = None
        a.recovery_started_at = None
        a.recovery_completed_at = None
        a.recovery_duration_minutes = None
        a.recovery_result = "pending"
        a.resulting_action_item_id = None
        a.re_engagement_anchor_at = None
        a.created_at = datetime.now(UTC)
        self._attempts[a.id] = a
        return a

    async def stamp_first_viewed(
        self, attempts: list[RecoveryAttempt], viewed_at: datetime
    ) -> None:
        for a in attempts:
            if a.first_viewed_at is None:
                a.first_viewed_at = viewed_at

    async def complete_for_action(
        self,
        user_id: UUID,
        action_item_id: UUID,
        *,
        completed_at: datetime,
        completion_status: str,
    ) -> RecoveryAttempt | None:
        # 실 repo 의 WHERE/UPDATE 는 test_recovery_completion 이 실 SQL 로 별도 고정.
        from reaction_backend.db.models.recovery_attempt import RECOVERY_SUCCESS_STATUSES

        attempt = next(
            (
                a
                for a in self._attempts.values()
                if a.user_id == user_id
                and a.resulting_action_item_id == action_item_id
                and a.recovery_result == "pending"
            ),
            None,
        )
        if attempt is None:
            return None
        if completion_status in RECOVERY_SUCCESS_STATUSES:
            attempt.recovery_result = "completed"
            attempt.recovery_completed_at = completed_at
            if attempt.recovery_started_at is not None:
                delta = completed_at - attempt.recovery_started_at
                attempt.recovery_duration_minutes = max(int(delta.total_seconds() // 60), 0)
        else:
            attempt.recovery_result = "abandoned"
        return attempt


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
    `_actions` 는 FakeActionItemRepo 와 공유 (fixture에서 주입) — `expire_unreflected`
    (#20 만료 cron)가 execution 으로 고른 카드를 action_items 쪽에서 변경하기 때문.
    """

    def __init__(self, actions: dict[UUID, ActionItem] | None = None) -> None:
        self._executions: dict[UUID, ExecutionEvent] = {}
        self._failure_tags: dict[UUID, list[str]] = {}
        self._blocks: dict[UUID, ScheduledBlock] = {}
        self._interruptions: dict[UUID, InterruptionEvent] = {}
        self._tag_master: list[FailureReasonTag] = default_failure_tags()
        self._actions: dict[UUID, ActionItem] = actions if actions is not None else {}

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

    async def action_ids_with_history(
        self, user_id: UUID, action_item_ids: Sequence[UUID]
    ) -> set[UUID]:
        """실행 이력이 있는 카드 id (실 repo 규칙 미러 — BE #214). 상태는 안 본다."""
        wanted = set(action_item_ids)
        return {
            e.action_item_id
            for e in self._executions.values()
            if e.user_id == user_id and e.action_item_id in wanted
        }

    async def find_open_block(self, user_id: UUID, action_item_id: UUID) -> ScheduledBlock | None:
        candidates = [
            b
            for b in self._blocks.values()
            if b.user_id == user_id
            and b.action_item_id == action_item_id
            and b.block_status in ("scheduled", "started")
        ]
        return min(candidates, key=lambda b: b.start_at) if candidates else None

    async def list_active_blocks_for_actions(
        self, user_id: UUID, action_item_ids: Sequence[UUID]
    ) -> list[tuple[UUID, str, datetime, datetime]]:
        """T1 미체크 배지 재료 (실 repo 미러 — 근거 대장 §6.2)."""
        wanted = set(action_item_ids)
        return [
            (b.action_item_id, b.block_status, b.start_at, b.end_at)
            for b in self._blocks.values()
            if b.user_id == user_id and b.action_item_id in wanted and b.block_status != "cancelled"
        ]

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

    async def list_blocks_starting_between(
        self, *, start: datetime, end: datetime
    ) -> list[ScheduledBlock]:
        # 실 repo 와 동일 의미: scheduled 만 · [start, end) · 카드 archived 제외.
        # (활성 사용자 필터는 SQL 전용 — test_pre_card_candidates_sql 이 실 SQL 로 고정.)
        found = []
        for b in self._blocks.values():
            if b.block_status != "scheduled" or not (start <= b.start_at < end):
                continue
            action = self._actions.get(b.action_item_id)
            if action is None or action.archived_at is not None:
                continue
            b.action_item = action  # 실 repo 는 joinedload — payload(제목)용
            found.append(b)
        return sorted(found, key=lambda b: b.start_at)

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

    async def get_open_pause(self, execution_id: UUID) -> InterruptionEvent | None:
        opens = [
            p
            for p in self._interruptions.values()
            if p.execution_id == execution_id
            and p.interruption_type == "user_pause"
            and p.resume_delay_minutes is None
            and p.resumed_after_interrupt is None
        ]
        return max(opens, key=lambda p: p.created_at) if opens else None

    async def create_pause(self, *, user_id: UUID, execution_id: UUID) -> InterruptionEvent:
        row = InterruptionEvent()
        row.id = uuid4()
        row.user_id = user_id
        row.execution_id = execution_id
        row.interruption_type = "user_pause"
        row.interruption_source = None
        row.resume_delay_minutes = None
        row.resumed_after_interrupt = None
        row.created_at = now_kst()
        self._interruptions[row.id] = row
        return row

    async def list_pending_reflection(
        self, user_id: UUID, *, since: datetime
    ) -> list[ExecutionEvent]:
        # 실 repo 의 `reflectable_from()` = greatest(plan_start_at, coalesce(actual, plan)).
        # 만료(expire_unreflected)와 **같은 식**이어야 정확한 여집합 (#20) — 아래 fake 도 동일.
        pending = [
            e
            for e in self._executions.values()
            if e.user_id == user_id
            and e.completion_status == "in_progress"
            and max(e.plan_start_at, e.actual_start_at or e.plan_start_at) >= since
        ]
        return sorted(pending, key=lambda e: e.plan_start_at)

    async def expire_unreflected(self, *, before: datetime, archived_at: datetime) -> int:
        """실 repo 의 WHERE 를 파이썬 술어로 재현 — `_FakeSession` 은 SQL 을 평가하지 않는다.

        실 구현(`ExecutionRepo.expire_unreflected`)과 조건이 어긋나면 테스트가 거짓 안심을
        주므로, 조건을 바꿀 땐 양쪽을 함께 고칠 것.
        """
        stale_action_ids = {
            e.action_item_id
            for e in self._executions.values()
            if e.completion_status == "in_progress"
            and max(e.plan_start_at, e.actual_start_at or e.plan_start_at) < before
        }
        live_block_action_ids = {
            b.action_item_id
            for b in self._blocks.values()
            if b.block_status in ("scheduled", "started") and b.start_at >= before
        }
        expired_ids = [
            a.id
            for a in self._actions.values()
            if a.id in stale_action_ids
            and a.id not in live_block_action_ids
            and a.archived_at is None
            and a.system_failure_reason is None
        ]
        for action_id in expired_ids:
            action = self._actions[action_id]
            action.system_failure_reason = "reflection_skipped"
            action.archived_at = archived_at
        for block in self._blocks.values():
            if block.action_item_id in expired_ids and block.block_status in (
                "scheduled",
                "started",
            ):
                block.block_status = "cancelled"
        return len(expired_ids)


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
        self._top_failure_contexts: list[TopFailureContext] = []

    # ── 테스트 보조 seed ──
    def seed_execution(self, stat: ExecutionStat) -> None:
        self._exec_stats.append(stat)

    def seed_recovery(self, stat: RecoveryStat) -> None:
        self._recovery_stats.append(stat)

    def seed_top_failure_context(self, ctx: TopFailureContext) -> None:
        self._top_failure_contexts.append(ctx)

    def seed_summary(self, summary: PeriodSummary) -> None:
        """이미 집계된 주간 요약을 심는다 (#168 정책 후보 입력)."""
        self._summaries[(summary.user_id, summary.start_date)] = summary

    # ── ReviewRepo 인터페이스 ──
    async def get_weekly(self, user_id: UUID, week_start: date) -> PeriodSummary | None:
        return self._summaries.get((user_id, week_start))

    async def get_latest_weekly(self, user_id: UUID) -> PeriodSummary | None:
        """가장 최근 주(start_date 최대) — 정책 후보 산출(#168) 입력."""
        mine = [v for (uid, _), v in self._summaries.items() if uid == user_id]
        return max(mine, key=lambda s: s.start_date) if mine else None

    async def collect_execution_stats(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[ExecutionStat]:
        return [s for s in self._exec_stats if start_dt <= s.plan_start_at < end_dt]

    async def collect_recovery_stats(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[RecoveryStat]:
        return list(self._recovery_stats)

    async def get_top_failure_contexts(
        self, user_id: UUID, d0: date, d1: date
    ) -> list[TopFailureContext]:
        return list(self._top_failure_contexts)

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
        self._meta: dict[UUID, tuple[str, str, UUID | None]] = {}
        self._action_repo: FakeActionItemRepo | None = None

    def link_actions(self, action_repo: FakeActionItemRepo) -> None:
        """재계획 조회(list_scheduled_between)가 ActionItem 을 되찾도록 action repo 를 연결."""
        self._action_repo = action_repo

    def seed(
        self,
        block: ScheduledBlock,
        *,
        title: str,
        category: str,
        goal_id: UUID | None = None,
    ) -> None:
        self._blocks[block.id] = block
        self._meta[block.id] = (title, category, goal_id)

    async def list_week(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[tuple[ScheduledBlock, str, str, UUID | None]]:
        # 실제 repo 와 동일하게 cancelled(계획 교체로 취소 등) 블록은 그리드에서 제외.
        rows = [
            (b, *self._meta[b.id])
            for b in self._blocks.values()
            if b.user_id == user_id
            and b.block_status != "cancelled"
            and start_dt <= b.start_at < end_dt
        ]
        return sorted(rows, key=lambda r: r[0].start_at)

    async def get_block(self, user_id: UUID, block_id: UUID) -> ScheduledBlock | None:
        b = self._blocks.get(block_id)
        if b is None or b.user_id != user_id:
            return None
        return b

    async def list_by_action_item(
        self, user_id: UUID, action_item_id: UUID
    ) -> list[ScheduledBlock]:
        rows = [
            b
            for b in self._blocks.values()
            if b.user_id == user_id
            and b.action_item_id == action_item_id
            and b.block_status != "cancelled"
        ]
        return sorted(rows, key=lambda b: b.start_at)

    async def create_block(
        self,
        *,
        user_id: UUID,
        action_item_id: UUID,
        start_at: datetime,
        end_at: datetime,
        source: str,
    ) -> ScheduledBlock:
        b = ScheduledBlock()
        b.id = uuid4()
        b.user_id = user_id
        b.action_item_id = action_item_id
        b.start_at = start_at
        b.end_at = end_at
        b.block_status = "scheduled"
        b.source = source
        b.external_calendar_event_id = None
        self._blocks[b.id] = b
        self._meta[b.id] = ("회복 블록", "recovery", None)
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

    async def list_scheduled_between(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[tuple[ScheduledBlock, ActionItem]]:
        """미착수('scheduled', source!='user_edit') 블록 + ActionItem — 재계획 재배치 대상.

        실 repo 의 join 규칙(#117) 미러: 시작/완료·user_edit·archived action 은 제외.
        ActionItem 은 `link_actions` 로 연결된 FakeActionItemRepo 에서 되찾는다.
        """
        rows: list[tuple[ScheduledBlock, ActionItem]] = []
        for b in self._blocks.values():
            if not (
                b.user_id == user_id
                and b.block_status == "scheduled"
                and b.source != "user_edit"
                and start_dt <= b.start_at < end_dt
            ):
                continue
            action = None
            if self._action_repo is not None:
                action = await self._action_repo.get_by_id(user_id, b.action_item_id)
            if action is not None:
                rows.append((b, action))
        return sorted(rows, key=lambda r: r[0].start_at)

    async def list_stale_scheduled_before(
        self, user_id: UUID, before_dt: datetime
    ) -> list[tuple[ScheduledBlock, ActionItem]]:
        """밀린 미착수 블록 + ActionItem — `list_scheduled_between` 과 필터 동일, 시간만 과거."""
        rows: list[tuple[ScheduledBlock, ActionItem]] = []
        for b in self._blocks.values():
            if not (
                b.user_id == user_id
                and b.block_status == "scheduled"
                and b.source != "user_edit"
                and b.start_at < before_dt
            ):
                continue
            action = None
            if self._action_repo is not None:
                action = await self._action_repo.get_by_id(user_id, b.action_item_id)
            if action is not None:
                rows.append((b, action))
        return sorted(rows, key=lambda r: r[0].start_at)

    async def list_committed_between(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[ScheduledBlock]:
        """확정(시작/완료 + user_edit) 블록 — 재계획이 회피할 busy (실 repo 규칙 미러)."""
        return [
            b
            for b in self._blocks.values()
            if b.user_id == user_id
            and b.block_status != "cancelled"
            and (b.block_status in ("started", "finished") or b.source == "user_edit")
            and b.start_at < end_dt
            and b.end_at > start_dt
        ]


class FakeInterviewRepo:
    """in-memory InterviewRepo — #6 배선. 세션 + 슬롯답 정규화 저장 미러."""

    def __init__(self) -> None:
        self._sessions: dict[UUID, InterviewSessionModel] = {}
        self._answers: dict[UUID, dict[str, InterviewSlotAnswer]] = {}

    async def create_session(
        self, user_id: UUID, llm_model: str, *, kind: str = "plan"
    ) -> InterviewSessionModel:
        s = InterviewSessionModel()
        s.id = uuid4()
        s.user_id = user_id
        s.llm_model = llm_model
        s.kind = kind
        s.total_turns = 0
        s.ambiguity_final = None
        s.end_reason = None
        s.ended_at = None
        s.used_fallback = False
        self._sessions[s.id] = s
        self._answers[s.id] = {}
        return s

    async def get_active_session(
        self, user_id: UUID, *, kind: str = "plan"
    ) -> InterviewSessionModel | None:
        for s in self._sessions.values():
            if s.user_id == user_id and s.kind == kind and s.end_reason is None:
                return s
        return None

    async def get_active(self, user_id: UUID, session_id: UUID) -> InterviewSessionModel | None:
        s = self._sessions.get(session_id)
        if s is None or s.user_id != user_id:
            return None
        return s

    async def get_latest_finished(
        self, user_id: UUID, *, kind: str = "plan"
    ) -> InterviewSessionModel | None:
        """정상 종료(abandoned 제외) `kind` 세션 중 ended_at 최신 1개 — 실 repo 와 동일 규칙."""
        finished = [
            s
            for s in self._sessions.values()
            if s.user_id == user_id
            and s.kind == kind
            and s.end_reason is not None
            and s.end_reason != "abandoned"
        ]
        with_ended = [s for s in finished if s.ended_at is not None]
        if with_ended:
            return max(with_ended, key=lambda s: s.ended_at)
        return finished[-1] if finished else None

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
        self,
        session: InterviewSessionModel,
        *,
        total_turns: int,
        ambiguity_final: float,
        used_fallback: bool = False,
    ) -> None:
        session.total_turns = total_turns
        session.ambiguity_final = ambiguity_final
        session.used_fallback = bool(session.used_fallback) or used_fallback

    async def finalize(
        self,
        session: InterviewSessionModel,
        *,
        end_reason: str,
        total_turns: int,
        ambiguity_final: float,
        used_fallback: bool = False,
    ) -> None:
        session.end_reason = end_reason
        session.total_turns = total_turns
        session.ambiguity_final = ambiguity_final
        session.used_fallback = bool(session.used_fallback) or used_fallback
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

    async def mark_discarded(self, draft: PlanDraft) -> PlanDraft:
        draft.status = "expired"
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
        user = self._by_id.get(user_id)
        if user is not None and getattr(user, "archived_at", None) is not None:
            return None
        return user

    async def get_by_email(self, email: str) -> User | None:
        user = self._by_email.get(email)
        if user is not None and getattr(user, "archived_at", None) is not None:
            return None
        return user

    async def list_active(self) -> list[User]:
        return [
            u
            for u in self._by_id.values()
            if u.onboarding_state == "ACTIVE" and not getattr(u, "is_anonymized", False)
        ]

    async def count_signed_up(self) -> int:
        return sum(1 for u in self._by_id.values() if getattr(u, "archived_at", None) is None)

    async def list_inactive_for_anonymization(self, *, before: datetime) -> list[User]:
        """90일 비활성 익명화 대상 (#24). 실 repo 와 같은 두 조건만 본다."""
        return [
            u
            for u in self._by_id.values()
            if u.last_active_at is not None
            and u.last_active_at < before
            and getattr(u, "anonymized_at", None) is None
        ]

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


class FakeInviteCodeRepo:
    """in-memory InviteCodeRepo — 가입 게이트 테스트용 (#324)."""

    def __init__(self) -> None:
        self._by_code: dict[str, InviteCode] = {}

    def seed(self, raw_code: str, *, used: bool = False, note: str | None = None) -> InviteCode:
        row = InviteCode()
        row.id = uuid4()
        row.code = normalize_code(raw_code)
        row.note = note
        row.used_at = now_kst() if used else None
        row.used_by_user_id = None
        self._by_code[row.code] = row
        return row

    async def get_by_code(self, raw_code: str) -> InviteCode | None:
        return self._by_code.get(normalize_code(raw_code))

    async def mark_used(self, row: InviteCode, *, used_by_user_id: UUID) -> None:
        row.used_at = now_kst()
        row.used_by_user_id = used_by_user_id

    async def create(self, raw_code: str, *, note: str | None = None) -> InviteCode:
        return self.seed(raw_code, note=note)

    async def list_all(self) -> list[InviteCode]:
        return list(self._by_code.values())


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
def fake_invite_code_repo() -> FakeInviteCodeRepo:
    return FakeInviteCodeRepo()


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
def fake_habit_instance_repo(fake_habit_repo: FakeHabitRepo) -> FakeHabitInstanceRepo:
    return FakeHabitInstanceRepo(habits=fake_habit_repo)


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
def fake_execution_repo(fake_action_item_repo: FakeActionItemRepo) -> FakeExecutionRepo:
    # 카드 스토어를 ActionItemRepo 와 공유 — 만료 cron(#20)이 execution 으로 고른 카드를
    # action_items 쪽에서 변경한다.
    return FakeExecutionRepo(actions=fake_action_item_repo._items)


@pytest.fixture
def fake_recovery_repo(fake_execution_repo: FakeExecutionRepo) -> FakeRecoveryRepo:
    # 실행/실패태그/카드 스토어를 ExecutionRepo 와 공유 — 체크인→복구 E2E 가능,
    # list_goal_outcomes(L3) 는 카드의 goal_id 를 알아야 한다.
    return FakeRecoveryRepo(
        executions=fake_execution_repo._executions,
        failure_tags=fake_execution_repo._failure_tags,
        actions=fake_execution_repo._actions,
    )


@pytest.fixture
def fake_daily_brief_repo() -> FakeDailyBriefRepo:
    return FakeDailyBriefRepo()


@pytest.fixture
def fake_review_repo() -> FakeReviewRepo:
    return FakeReviewRepo()


class FakeProfileRepo:
    """in-memory ProfileRepo — behavioral_profiles / interaction_styles (#A-1·A-2)."""

    def __init__(self) -> None:
        self._behavioral: dict[UUID, BehavioralProfile] = {}
        self._interaction: dict[UUID, InteractionStyle] = {}

    async def get_behavioral(self, user_id: UUID) -> BehavioralProfile | None:
        return self._behavioral.get(user_id)

    async def get_interaction(self, user_id: UUID) -> InteractionStyle | None:
        return self._interaction.get(user_id)

    async def upsert_behavioral(
        self, user_id: UUID, *, fields: dict[str, Any]
    ) -> BehavioralProfile:
        row = self._behavioral.get(user_id)
        if row is None:
            row = BehavioralProfile()
            row.user_id = user_id
            # server_default 미러 (테스트에선 flush 없이 읽으므로 명시)
            row.energy_cycle = "varies"
            row.attention_span = 30
            row.time_chunk_preference = "30"
            row.preferred_start_time = None
            row.preferred_end_time = None
            self._behavioral[user_id] = row
        for key, value in fields.items():
            if value is not None:
                setattr(row, key, value)
        return row

    async def upsert_interaction(
        self, user_id: UUID, *, fields: dict[str, Any]
    ) -> InteractionStyle:
        row = self._interaction.get(user_id)
        if row is None:
            row = InteractionStyle()
            row.user_id = user_id
            row.recovery_tone = "normal"
            row.suggestion_style = "neutral"
            row.explanation_depth = "normal"
            row.reminder_frequency = "standard"
            self._interaction[user_id] = row
        for key, value in fields.items():
            if value is not None:
                setattr(row, key, value)
        return row


@pytest.fixture
def fake_profile_repo() -> FakeProfileRepo:
    return FakeProfileRepo()


class FakePolicySnapshotRepo:
    """in-memory PolicySnapshotRepo — #83 §14 + #168 (생산 경로)."""

    def __init__(self) -> None:
        self._items: list[PolicySnapshot] = []

    def seed(self, snapshot: PolicySnapshot) -> None:
        self._items.append(snapshot)

    def all_of(self, user_id: UUID) -> list[PolicySnapshot]:
        """테스트가 저장 결과를 직접 볼 때."""
        return [s for s in self._items if s.user_id == user_id]

    async def get_active(self, user_id: UUID) -> PolicySnapshot | None:
        actives = [s for s in self._items if s.user_id == user_id and s.is_active]
        return max(actives, key=lambda s: s.version) if actives else None

    async def list_history(self, user_id: UUID) -> list[PolicySnapshot]:
        return sorted(self.all_of(user_id), key=lambda s: s.version, reverse=True)

    async def get_by_version(self, user_id: UUID, version: int) -> PolicySnapshot | None:
        return next((s for s in self.all_of(user_id) if s.version == version), None)

    async def next_version(self, user_id: UUID) -> int:
        versions = [s.version for s in self.all_of(user_id)]
        return (max(versions) if versions else 0) + 1

    async def create_active(
        self,
        user_id: UUID,
        *,
        behavioral_profile: dict[str, Any],
        execution_constraints: dict[str, Any],
        interaction_style: dict[str, Any],
        recovery_policy: dict[str, Any],
        source: str,
        reason_for_update: str | None,
        now: datetime,
        prompt_version: str | None = None,
    ) -> PolicySnapshot:
        for previous in self.all_of(user_id):
            if previous.is_active:
                previous.is_active = False
                previous.valid_to = now
        snapshot = PolicySnapshot()
        snapshot.user_id = user_id
        snapshot.version = await self.next_version(user_id)
        snapshot.is_active = True
        snapshot.behavioral_profile = behavioral_profile
        snapshot.execution_constraints = execution_constraints
        snapshot.interaction_style = interaction_style
        snapshot.recovery_policy = recovery_policy
        snapshot.source = source
        snapshot.reason_for_update = reason_for_update
        snapshot.prompt_version = prompt_version
        snapshot.valid_from = now
        snapshot.valid_to = None
        self._items.append(snapshot)
        return snapshot


@pytest.fixture
def fake_policy_snapshot_repo() -> FakePolicySnapshotRepo:
    return FakePolicySnapshotRepo()


@pytest.fixture
def fake_scheduled_block_repo() -> FakeScheduledBlockRepo:
    return FakeScheduledBlockRepo()


@pytest.fixture
def fake_plan_draft_repo() -> FakePlanDraftRepo:
    return FakePlanDraftRepo()


@pytest.fixture
def fake_sessions() -> list[_FakeSession]:
    """`client` 의 요청들이 만든 `_FakeSession` 목록 — commit 이 계약인 라우트 검증용.

    `_FakeSession.commit_count` 는 진작 있었지만 HTTP 경로에서는 세션을 잡을 수 없어
    "commit 을 지워도 전 스위트 초록" 뮤턴트를 못 잡았다. 이 픽스처가 그 구멍을 메운다.
    """
    return []


@pytest.fixture
def client(
    demo_user_orm: User,
    fake_time_policy_repo: FakeTimePolicyRepo,
    fake_fixed_schedule_repo: FakeFixedScheduleRepo,
    fake_notification_repo: FakeNotificationRepo,
    fake_notification_send_repo: FakeNotificationSendRepo,
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
    fake_policy_snapshot_repo: FakePolicySnapshotRepo,
    fake_profile_repo: FakeProfileRepo,
    fake_sessions: list[_FakeSession],
) -> Iterator[TestClient]:
    """기본 client — 인증된 demo user + 도메인 fake repo + fake session."""
    _reset_process_singletons()
    fake_user_repo.register(demo_user_orm)
    # 재계획 조회는 block↔action repo 상호 참조(join/백로그 필터)를 쓴다.
    fake_scheduled_block_repo.link_actions(fake_action_item_repo)
    fake_action_item_repo.link_blocks(fake_scheduled_block_repo)
    app = create_app()

    async def _fake_session_gen() -> AsyncIterator[_FakeSession]:
        # commit 이 계약인 라우트를 검증할 수 있게 만들어진 세션을 모아 둔다 —
        # 요청마다 새로 만들고 버리면 "commit 을 지워도 전 스위트 초록" 뮤턴트를 못 잡는다
        # (conftest `_FakeSession.commit_count` 주석의 #20 리뷰와 같은 계열).
        session = _FakeSession()
        fake_sessions.append(session)
        yield session

    app.dependency_overrides[get_current_user] = lambda: demo_user_orm
    app.dependency_overrides[get_db] = _fake_session_gen
    app.dependency_overrides[get_time_policy_repo] = lambda: fake_time_policy_repo
    app.dependency_overrides[get_fixed_schedule_repo] = lambda: fake_fixed_schedule_repo
    app.dependency_overrides[get_notification_repo] = lambda: fake_notification_repo
    app.dependency_overrides[get_notification_send_repo] = lambda: fake_notification_send_repo
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
    app.dependency_overrides[get_policy_snapshot_repo] = lambda: fake_policy_snapshot_repo
    app.dependency_overrides[get_profile_repo] = lambda: fake_profile_repo
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
def auth_client(
    fake_user_repo: FakeUserRepo,
    fake_invite_code_repo: FakeInviteCodeRepo,
    fake_privacy_repo: FakePrivacyRepo,
) -> Iterator[TestClient]:
    """`/auth/*` 테스트 — repo/session 만 override, 인증은 실제 JWT 흐름.

    `fake_privacy_repo` 도 override 한다(#321) — `/settings/delete-account` 가 실제
    `get_current_user`(→ `fake_user_repo`) 로 인증된 뒤 access/refresh token 이 죽는지까지
    한 클라이언트로 이어 테스트하려면, 계정 삭제 자체를 이 client 로 호출할 수 있어야 한다.
    """
    _reset_process_singletons()
    app = create_app()

    async def _fake_session_gen() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    app.dependency_overrides[get_db] = _fake_session_gen
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_invite_code_repo] = lambda: fake_invite_code_repo
    app.dependency_overrides[get_privacy_repo] = lambda: fake_privacy_repo
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


# ── 실 DB 픽스처 (P1, docs/experiments/experiment-plan-v1.md §1) ───────────
#
# 위의 `client`/fake repo 들과는 다른 층이다 — 저것들은 라우터를 fake 로 전면 격리해서
# 빠르지만, "이 SQL 이 실제 Postgres 에서 이 값을 내는가"는 fake 로는 원리적으로 답할 수
# 없다(fake 자체가 그 답을 흉내내고 있으므로). 이 픽스처는 진짜 DB 에 진짜 SQL 을 태운다.
#
# DATABASE_URL 이 없으면 스킵 — `tests/test_db.py::DB_AVAILABLE` 와 같은 게이트를 그대로
# 따른다. 로컬에서 DB 없이 전체 스위트가 통과해야 한다는 기존 관례를 깨지 않는다.


def _db_available() -> bool:
    from reaction_backend.config import get_settings

    return bool(get_settings().database_url)


DB_AVAILABLE = _db_available()


@pytest.fixture
async def real_db_session() -> AsyncIterator[Any]:
    """진짜 Postgres 세션 — 테스트 종료 시 트랜잭션 롤백으로 격리.

    DATABASE_URL 이 없으면 스킵한다(CI 의 `lint-test` 잡에만 postgres 서비스가 있고,
    로컬은 기본적으로 없다 — 로컬에서 돌리려면 개발자가 직접 DATABASE_URL 을 세팅하고
    `uv run alembic upgrade head` 로 스키마를 올려 둘 것).

    ⚠️ 이 세션으로 **`session.commit()` 을 부르지 말 것** — 부르는 순간 이 픽스처가 감싼
    바깥 트랜잭션이 끝나 버려서, 테스트가 끝나도 롤백이 무력화되고 다음 테스트로 데이터가
    샌다. 같은 트랜잭션 안에서는 `flush()` 만으로 그 이후의 SELECT 에 보이므로, 시드 후
    조회하는 용도(P1 SQL 핀 테스트)엔 `flush()` 로 충분하다. `commit()` 이 필요한 코드
    경로(라우터 등)를 이 세션으로 통합 테스트하려면 nested-savepoint 패턴이 별도로
    필요하다 — 지금은 그 범위가 아니다.

    ⚠️ **앱의 `get_engine()`(lru_cache 싱글턴)을 재사용하지 않는다.** `pytest-asyncio` 는
    테스트 함수마다 새 이벤트 루프를 연다(기본 function 스코프) — asyncpg 커넥션은 만들어진
    루프에 묶이므로, 한 테스트에서 연 풀 커넥션을 다음 테스트(다른 루프)가 재사용하면
    "Event loop is closed" 로 죽는다. 테스트마다 **독립된 엔진을 만들고 끝나면 dispose**
    해서 이 문제를 원천 차단한다 — 앱 싱글턴을 테스트 이벤트 루프로 오염시키지도 않는다.
    """
    if not DB_AVAILABLE:
        pytest.skip("DATABASE_URL not set — real DB fixture skipped")

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from reaction_backend.config import get_settings
    from reaction_backend.db.session import normalize_async_url

    url = normalize_async_url(get_settings().database_url)
    # NullPool — 커넥션을 재사용하지 않는다. 이 엔진은 테스트 1건당 만들고 버리므로
    # 앱의 장수 pool(pool_pre_ping/recycle)이 여기서는 의미가 없고, 풀링 없이 단일
    # 커넥션만 열고 닫는 편이 이벤트 루프 경계에서 더 깨끗하게 정리된다.
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            session = AsyncSession(bind=conn, expire_on_commit=False)
            try:
                yield session
            finally:
                await session.close()
                await trans.rollback()
    finally:
        await engine.dispose()


__all__ = [
    "DEMO_USER_UUID",
    "DB_AVAILABLE",
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
    "FakeNotificationSendRepo",
    "FakeWebPushSender",
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
    "real_db_session",
    "unauthed_client",
]
