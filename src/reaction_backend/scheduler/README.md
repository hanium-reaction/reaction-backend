# `scheduler/` — 시간 트리거 / cron / 배치

후속 이슈(#1 follow-up / #6)에서 채워진다. 후보 라이브러리: **APScheduler** (단순) 또는 **Arq** (Redis 기반, 분산 가능).

cron 시간표 (사용자 timezone 기준 — DevBaseline + DB 시나리오 분석):

| 시각 | 작업 | 출력 |
| --- | --- | --- |
| 매일 06:00 | `daily_brief_precompute` — 헤드라인 + Big Rock 생성 (LLM 1회) | `daily_briefs` row |
| 매일 21:00 | `evening_reflection_notify` — 회고 알림 발송 (예산 enforce) | push notification |
| 매주 일요일 03:00 | `weekly_review_precompute` — KPI + insight 생성 (LLM 1회) | `period_summaries` row |
| 매주 월요일 00:00 | `habit_instances_generator` — 이번 주 habit_instances 행 생성 | `habit_instances` rows |
| 6시간마다 | `interruption_resolver` — `resumed_after_interrupt IS NULL AND created_at < now()-6h` → `false` | `interruption_events` UPDATE |
| 매일 04:00 KST | `anonymize_inactive_users` — last_active_at < now()-90d → 익명화 | `users` UPDATE |
| 1시간마다 | `oauth_token_refresher` — 만료 임박 토큰 갱신 | `calendar_connections` UPDATE |
| 5분마다 | `notification_dispatcher` — 예약된 알림 발송 | (외부) Web Push |

규약: 모든 cron은 **idempotent** 해야 한다. 1회 실행 보장 X, 다회 실행 안전성 O.
