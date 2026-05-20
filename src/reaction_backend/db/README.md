# `db/` — DB 연결 / ORM 모델 / 마이그레이션

DB는 **Supabase / Postgres** (계획 문서 기준). Issue #2 (DB Schema v0.7 Migration) 에서 채워진다.

후속 이슈에서 추가될 모듈:
- `session.py` — SQLAlchemy async session, dependency
- `base.py` — DeclarativeBase
- `models/` — 26 테이블 ORM 모델 (Reaction_DB_설계서_v0.7.1 기준)
  - `users.py`, `goals.py`, `goal_nodes.py`, `action_items.py`, `scheduled_blocks.py`,
    `habits.py`, `habit_instances.py`, `time_policies.py`, `fixed_schedules.py`,
    `behavioral_profiles.py`, `interaction_styles.py`, `interview_sessions.py`,
    `interview_slot_answers.py`, `calendar_connections.py`, `execution_events.py`,
    `interruption_events.py`, `context_snapshots.py`, `execution_failure_tags.py`,
    `failure_reason_tags.py`, `recovery_attempts.py`, `recovery_strategy_catalog.py`,
    `daily_briefs.py`, `period_summaries.py`, `policy_snapshots.py`,
    `notification_settings.py`, `llm_runs.py`, `idempotency_keys.py`
- `../../../alembic/` (또는 supabase migrations) — 마이그레이션 스크립트

규약:
- 모든 시간 컬럼은 **UTC timestamptz**. 응답 시 KST로 변환 ([`../schemas/common.py`](../schemas/common.py) 의 `now_kst` 참고)
- 토큰/메모는 컬럼 암호화 (`*_encrypted` 접미사)
- soft delete only (`archived_at`). hard delete 금지.
