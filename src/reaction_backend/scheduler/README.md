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
| 6시간마다 | `expire_stale_drafts` — `plan_drafts.status='draft' AND expires_at < now()` → `expired` (72h, §7.8) | `plan_drafts` UPDATE |
| 매일 04:00 KST | `expire_unreflected_cards` — 회고 창(3일) 밖 미체크 실행의 카드 → `system_failure_reason='reflection_skipped'` + `archived_at` + 미종결 블록 cancel | `action_items` / `scheduled_blocks` UPDATE |
| 매일 04:00 KST | `anonymize_inactive_users` — last_active_at < now()-90d → 익명화 | `users` UPDATE |
| 1시간마다 | `oauth_token_refresher` — 만료 임박 토큰 갱신 | `calendar_connections` UPDATE |
| 5분마다 | `notification_dispatcher` — 예약된 알림 발송 | (외부) Web Push |

규약: 모든 cron은 **idempotent** 해야 한다. 1회 실행 보장 X, 다회 실행 안전성 O.

## 구현 상태

| job 함수 | 모듈 | 이슈 | 상태 |
| --- | --- | --- | --- |
| `run_morning_brief_for_user(user_id, now_kst_dt, *, action_repo, brief_repo, session)` | `morning_brief.py` | #19-C | ✅ job 로직 (룰+`aiClient.run("brief/morning_brief")` fallback, 같은 날 skip) |
| `run_interruption_resolver(now_kst_dt, *, repo)` | `interruption_resolver.py` | #19-C | ✅ job 로직 (6h 미재개 NULL→false) |
| `run_expire_stale_drafts(session, *, now, repo)` | `expire_drafts.py` | #62 | ✅ job 로직 (72h 미응답 Draft expired, idempotent) |
| `run_weekly_review_for_user(user_id, week_start, now_kst_dt, *, repo, force=False)` | `weekly_review_precompute.py` | #21-A | ✅ job 로직 (룰 KPI 집계 → `period_summaries` upsert, 같은 주 skip) |
| `run_expire_unreflected_cards(session, *, now, repo)` | `expire_reflections.py` | #20 | ✅ job 로직 (회고 창 밖 미체크 카드 만료, idempotent). 창 경계 `pending_reflection_since(today)` 는 **라우터도 재사용하는 단일 소스** — `GET /reflection/pending` 이 `>=`, cron 이 `<` (정확한 여집합) |

## 런타임 (#24)

| 모듈 | 역할 |
| --- | --- |
| `sweeps.py` | **전체 활성 사용자 순회 wrapper** — `run_morning_brief_sweep` / `run_weekly_review_sweep`. per-user job 을 `user_repo.list_active()` 전체에 실행(개별 try/except 격리, 사용자 톤 반영). |
| `runtime.py` | **APScheduler(AsyncIOScheduler) 등록** — `build_scheduler()` 가 5 job 을 KST cron 으로 add_job. job wrapper 가 1회용 세션·repo 를 만들어 sweep/전역 job 호출. |

등록 시각: morning_brief=매일 06:00 · weekly_review=일요일 03:00 · interruption_resolver=6h · expire_drafts=6h · expire_reflections=매일 04:00.

기동: `main.py` lifespan 이 **`SCHEDULER_ENABLED=true`** 일 때만 `build_scheduler().start()`.
기본 OFF — 테스트/로컬은 안 돈다(데모는 시드로 커버).

> ⚠️ **in-process** — 다중 인스턴스 배포 시 중복 실행(모든 job idempotent → 안전, 단일 인스턴스 권장).
> Render 배포 시 `SCHEDULER_ENABLED=true` + 단일 인스턴스 = #24 PM 배포 설정.
> 미등록 job(anonymize_inactive=#15 / habit_instances_generator / notification_dispatcher 등)은
> job 함수 구현 후 `runtime.build_scheduler` 에 add_job 추가.
