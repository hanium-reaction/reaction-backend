"""ORM 모델 — Reaction_DB_설계서_v0.7.1 + DB 시나리오별 상세분석 기반.

Alembic이 `Base.metadata` 를 통해 자동 발견하려면 모든 모델이 여기서 import 되어야 한다.
도메인 라우터/repository는 `from reaction_backend.db.models import User` 형태로 사용.

PR 2-B 범위 (사용자/온보딩 8 테이블):
- users · interview_sessions · interview_slot_answers
- behavioral_profiles · interaction_styles · notification_settings
- calendar_connections · fixed_schedules

PR 2-C 이후로 미룬 모델:
- goals · goal_nodes · habits · habit_instances · action_items · scheduled_blocks
- time_policies · dependency_links · inbox_items
- execution_events · interruption_events · context_snapshots
- execution_failure_tags · failure_reason_tags · recovery_strategy_catalog · recovery_attempts
- period_summaries · daily_briefs · policy_snapshots · llm_runs · idempotency_keys
"""

from reaction_backend.db.models.behavioral_profile import BehavioralProfile
from reaction_backend.db.models.calendar_connection import CalendarConnection
from reaction_backend.db.models.fixed_schedule import FixedSchedule
from reaction_backend.db.models.interaction_style import InteractionStyle
from reaction_backend.db.models.interview_session import InterviewSession
from reaction_backend.db.models.interview_slot_answer import InterviewSlotAnswer
from reaction_backend.db.models.notification_setting import NotificationSetting
from reaction_backend.db.models.user import User

__all__ = [
    "BehavioralProfile",
    "CalendarConnection",
    "FixedSchedule",
    "InteractionStyle",
    "InterviewSession",
    "InterviewSlotAnswer",
    "NotificationSetting",
    "User",
]
