"""ORM 모델 — Reaction_DB_설계서_v0.7.1 + DB 시나리오별 상세분석 기반.

Alembic이 `Base.metadata` 를 통해 자동 발견하려면 모든 모델이 여기서 import 되어야 한다.
도메인 라우터/repository는 `from reaction_backend.db.models import User` 형태로 사용.

PR 2-B 범위 (사용자/온보딩 8 테이블):
- users · interview_sessions · interview_slot_answers
- behavioral_profiles · interaction_styles · notification_settings
- calendar_connections · fixed_schedules

PR 2-C 범위 (계획 9 테이블):
- time_policies (2-B 누락분 보강)
- goals · goal_nodes
- habits · habit_instances
- inbox_items · action_items · scheduled_blocks · dependency_links

PR 2-D 범위 (실행/회복 7 테이블):
- execution_events · interruption_events · context_snapshots
- failure_reason_tags (master) · execution_failure_tags
- recovery_strategy_catalog (master) · recovery_attempts

PR 2-E 범위 (집계/시스템 5 테이블 + seed + 문서):
- period_summaries (주간/월간 KPI, S21)
- daily_briefs (v0.7, 06:00 cron 캐시)
- policy_snapshots (PolicySnapshot 버전 이력)
- llm_runs (모든 LLM 호출 로그)
- idempotency_keys (24h 멱등성 캐시)
+ failure_reason_tags 13 seed + recovery_strategy_catalog 9 seed (data migration)
+ scripts/db_reset.py + scripts/db_seed_demo.py
+ docs/erd-diff.md

= 합계 29 도메인 테이블.
"""

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.behavioral_profile import BehavioralProfile
from reaction_backend.db.models.calendar_connection import CalendarConnection
from reaction_backend.db.models.context_snapshot import ContextSnapshot
from reaction_backend.db.models.daily_brief import DailyBrief
from reaction_backend.db.models.dependency_link import DependencyLink
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.execution_failure_tag import ExecutionFailureTag
from reaction_backend.db.models.failure_reason_tag import FailureReasonTag
from reaction_backend.db.models.fixed_schedule import FixedSchedule
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.habit import Habit
from reaction_backend.db.models.habit_instance import HabitInstance
from reaction_backend.db.models.idempotency_key import IdempotencyKey
from reaction_backend.db.models.inbox_item import InboxItem
from reaction_backend.db.models.interaction_style import InteractionStyle
from reaction_backend.db.models.interruption_event import InterruptionEvent
from reaction_backend.db.models.interview_session import InterviewSession
from reaction_backend.db.models.interview_slot_answer import InterviewSlotAnswer
from reaction_backend.db.models.llm_run import LlmRun
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

__all__ = [
    "ActionItem",
    "BehavioralProfile",
    "CalendarConnection",
    "ContextSnapshot",
    "DailyBrief",
    "DependencyLink",
    "ExecutionEvent",
    "ExecutionFailureTag",
    "FailureReasonTag",
    "FixedSchedule",
    "Goal",
    "GoalNode",
    "Habit",
    "HabitInstance",
    "IdempotencyKey",
    "InboxItem",
    "InteractionStyle",
    "InterruptionEvent",
    "InterviewSession",
    "InterviewSlotAnswer",
    "LlmRun",
    "NotificationSend",
    "NotificationSetting",
    "PeriodSummary",
    "PlanDraft",
    "PolicySnapshot",
    "RecoveryAttempt",
    "RecoveryStrategyCatalog",
    "ScheduledBlock",
    "TimePolicy",
    "User",
    "UserConsent",
]
