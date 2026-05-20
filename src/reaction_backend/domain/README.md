# `domain/` — 순수 도메인 모델

이 레이어는 **프레임워크 의존성 없는 비즈니스 객체**만 둔다. SQLAlchemy/Pydantic/FastAPI에 의존하지 않는 entity, value object, domain service가 위치한다.

후속 이슈에서 추가될 모듈 예시:
- `goal.py`, `goal_node.py`, `action_item.py`, `scheduled_block.py`
- `interview_session.py`, `interview_slot.py` (19개 슬롯 카탈로그)
- `execution_event.py`, `interruption_event.py`, `context_snapshot.py`
- `recovery_attempt.py`, `failure_reason.py` (13종 enum), `recovery_strategy.py` (9종 catalog)
- `policy_snapshot.py`, `behavioral_profile.py`, `interaction_style.py`
- `time_policy.py` (discriminated union: sleep / lunch / break_min / no_touch / late_night_block / custom)

> ⚠️ DB row와 1:1 매핑이 아니다. ORM 모델은 [`../db/`](../db/) 에 둔다.
