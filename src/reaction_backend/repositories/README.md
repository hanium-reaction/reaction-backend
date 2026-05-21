# `repositories/` — Repository 패턴

도메인 단위로 DB 접근을 캡슐화한다. 라우터/에이전트는 ORM 직접 import 금지, 이 레이어를 거친다.

후속 이슈에서 추가될 모듈 (Issue #2):
- `users_repo.py`, `goals_repo.py`, `action_items_repo.py`, `scheduled_blocks_repo.py`
- `interview_repo.py` — `upsert_slot_answer(session_id, slot_key, value, clarity_score)`
- `execution_repo.py` — `start`, `pause`, `resume`, `check_in` 단일 트랜잭션
- `recovery_repo.py`, `period_summaries_repo.py`, `policy_repo.py`
- `idempotency_repo.py` — 24h key 체크/저장

규약: 모든 write 메서드는 **단일 트랜잭션** 단위. 정책 위반 감지 시 즉시 rollback.
