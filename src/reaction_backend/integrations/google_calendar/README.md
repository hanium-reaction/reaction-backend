# `integrations/google_calendar/` — Google Calendar

MVP 스코프: **read-only freebusy + 사용자 승인 후 events.insert**. write-back은 P1.

후속 모듈:
- `client.py` — google-api-python-client async wrapper (refresh 자동 처리)
- `freebusy.py` — `freebusy.query` 캐싱 (60s TTL)
- `events.py` — `events.insert` (Idempotency-Key 필수, externalCalendarEventId 가드)
- `token_store.py` — calendar_connections 읽기/쓰기 (at-rest 암호화)

규약:
- 권한 박탈 / refresh 실패 → `revoked_at` set + 다음 진입 시 재연결 안내
- 같은 scheduledBlockId에 대한 중복 insert는 서버 측 가드 (externalCalendarEventId 존재 시 skip)
- API quota 초과는 exponential backoff + circuit breaker
