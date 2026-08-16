# `scheduler/` — 시간 트리거 / cron / 배치

후속 이슈(#1 follow-up / #6)에서 채워진다. 후보 라이브러리: **APScheduler** (단순) 또는 **Arq** (Redis 기반, 분산 가능).

cron 시간표 (사용자 timezone 기준 — DevBaseline + DB 시나리오 분석):

| 시각 | 작업 | 출력 |
| --- | --- | --- |
| 매일 06:00 | `daily_brief_precompute` — 헤드라인 + Big Rock 생성 (LLM 1회) | `daily_briefs` row |
| 19~23시 5분 폴 | `evening_reflection_notify` — 사용자별 설정 시각 이후 회고 알림 (pending 있을 때만, 게이트 enforce) | (외부) Web Push + `notification_sends` row |
| 종일 5분 폴 | `pre_card_notify` — 2~7분 뒤 시작 블록 사전 알림 (opt-in, 게이트 enforce) | (외부) Web Push + `notification_sends` row |
| 매주 일요일 03:00 | `weekly_review_precompute` — KPI + insight 생성 (LLM 1회) | `period_summaries` row |
| 매일 00:05 | `habit_instances` — 이번 주 habit_instances 행 생성 (월요일에만 실제 생성, 그 외 no-op) | `habit_instances` rows |
| 6시간마다 | `interruption_resolver` — `resumed_after_interrupt IS NULL AND created_at < now()-6h` → `false` | `interruption_events` UPDATE |
| 6시간마다 | `expire_stale_drafts` — `plan_drafts.status='draft' AND expires_at < now()` → `expired` (72h, §7.8) | `plan_drafts` UPDATE |
| 매일 04:00 KST | `expire_unreflected_cards` — 회고 창(3일) 밖 미체크 실행의 카드 → `system_failure_reason='reflection_skipped'` + `archived_at` + 미종결 블록 cancel | `action_items` / `scheduled_blocks` UPDATE |
| 매일 04:00 KST | `abandon_stale_recoveries` — 회고 창 밖 미완주 회복 → `recovery_result='abandoned'` (같은 job 안에서 만료 뒤 실행) | `recovery_attempts` UPDATE |
| 매일 04:00 KST | `expire_stale_proposed_goals` — 잠정(proposed) 목표 중 14일 지나도 승격 안 된 것 → `archived` (#178) | `goals` UPDATE |
| 매일 04:00 KST | `anonymize_inactive_users` — last_active_at < now()-90d → 익명화 | `users` UPDATE |
| 1시간마다 | `oauth_token_refresher` — 만료 임박 토큰 갱신 | `calendar_connections` UPDATE |
| ~~5분마다~~ | ~~`notification_dispatcher` — 예약된 알림 발송~~ — **발송 게이트로 대체** (`safety/push_gate.py`, ADR-0006 §1: 큐 없이 cron → 게이트 직접발송, enforce 지점은 게이트 단일) | — |

규약: 모든 cron은 **idempotent** 해야 한다. 1회 실행 보장 X, 다회 실행 안전성 O.

## 구현 상태

| job 함수 | 모듈 | 이슈 | 상태 |
| --- | --- | --- | --- |
| `run_morning_brief_for_user(user_id, now_kst_dt, *, action_repo, brief_repo, session)` | `morning_brief.py` | #19-C | ✅ job 로직 (룰+`aiClient.run("brief/morning_brief")` fallback, 같은 날 skip) |
| `run_interruption_resolver(now_kst_dt, *, repo)` | `interruption_resolver.py` | #19-C | ✅ job 로직 (6h 미재개 NULL→false) |
| `run_expire_stale_drafts(session, *, now, repo)` | `expire_drafts.py` | #62 | ✅ job 로직 (72h 미응답 Draft expired, idempotent) |
| `run_weekly_review_for_user(user_id, week_start, now_kst_dt, *, repo, force=False)` | `weekly_review_precompute.py` | #21-A | ✅ job 로직 (룰 KPI 집계 → `period_summaries` upsert, 같은 주 skip) |
| `run_expire_unreflected_cards(session, *, now, repo)` | `expire_reflections.py` | #20 | ✅ job 로직 (회고 창 밖 미체크 카드 만료, idempotent). 창 경계 `pending_reflection_since(today)` 는 **라우터도 재사용하는 단일 소스** — `GET /reflection/pending` 이 `>=`, cron 이 `<` (정확한 여집합) |
| `run_abandon_stale_recoveries(session, *, now, repo)` | `expire_reflections.py` | #20 | ✅ job 로직 (회고 창 밖 미완주 회복 포기, idempotent). 만료 cron 과 **경계값도 기준식도 같은 소스** — `pending_reflection_since(today)` + `execution_repo.reflectable_from()`. 두 쪽이 다른 컬럼을 재면 아직 회고 가능한 회복이 포기로 확정돼 `average_recovery_minutes` 가 사라진다 |
| `run_evening_reflection_notify_sweep(now, *, user_repo, notif_repo, execution_repo, send_repo, sender, session)` | `notify_sweeps.py` | #20 | ✅ 사용자별 `evening_reflection_time` 이후 첫 폴에서 발송 (pending 있을 때만 — 창 경계는 위와 동일 소스). 발송 판단은 전부 `safety/push_gate.py` (주 ≤3건·23~07 금지·클래스 하루 1건) |
| `run_pre_card_notify_sweep(now, *, execution_repo, notif_repo, send_repo, sender, session)` | `notify_sweeps.py` | #20 | ✅ `[now+2m, now+7m)` 시작 `scheduled` 블록 → opt-in 사용자에게 사전 알림. 동일 게이트 경유 |
| `run_habit_instances_sweep(week_start, *, user_repo, habit_repo, instance_repo, session)` | `habit_instances.py` | #22 | ✅ 활성 사용자 × 활성 습관의 그 주 인스턴스 생성, idempotent(`(habit_id, week_start)` UNIQUE + get-or-create). `week_start` 는 호출자 주입 — 런타임 job 이 `GET /today/agenda` 와 **같은 함수** `habit_repo.current_week_start_kst()` 를 쓴다(생성/조회 주 경계 단일 소스). `target_count` 는 `habit.target_count`(S22 `apply_penalty` 가 쓰는 자리) |
| `run_expire_stale_proposed_goals(session, *, now, repo)` | `expire_proposed_goals.py` | #178 | ✅ job 로직 (TTL 14일 지난 proposed 목표 보관, idempotent). 경계 `proposed_goal_stale_before(now)` 는 프리뷰 스크립트(`scripts/preview_expire_proposed_goals.py`)도 재사용하는 단일 소스. 사용자 알림 없음(ADR-0005 §7.8 선례 — 알림 4번째 클래스는 AGENTS §1 잠금이라 §8 대상) |

## 런타임 (#24)

| 모듈 | 역할 |
| --- | --- |
| `sweeps.py` | **전체 활성 사용자 순회 wrapper** — `run_morning_brief_sweep` / `run_weekly_review_sweep`. per-user job 을 `user_repo.list_active()` 전체에 실행(개별 try/except 격리, 사용자 톤 반영). |
| `habit_instances.py` | **전체 활성 사용자 × 활성 습관 순회** — `run_habit_instances_sweep`. `notify_sweeps` 와 같은 트랜잭션 규약(사용자 단위 commit + except rollback). |
| `runtime.py` | **APScheduler(AsyncIOScheduler) 등록** — `build_scheduler()` 가 9 job 을 KST cron 으로 add_job. job wrapper 가 1회용 세션·repo 를 만들어 sweep/전역 job 호출. |

등록 시각: morning_brief=매일 06:00 · weekly_review=일요일 03:00 · interruption_resolver=6h ·
expire_drafts=6h · expire_reflections=매일 04:00 · expire_proposed_goals=매일 04:00 ·
evening_reflection_notify=19~23시 */5분 · pre_card_notify=종일 */5분 · habit_instances=매일 00:05.

> **`habit_instances` 가 "매주 월요일"이 아니라 "매일"인 이유**: 주 1회 job 은 놓치면 다음
> 기회가 일주일 뒤다. jobstore 가 MemoryJobStore 라 재기동 시 `next_run_time` 을 now 기준으로
> 다시 잡으므로, 월요일 00:05 에 앱이 떠 있지 않으면(배포·재부팅) `misfire_grace_time` 으로도
> 회수되지 않고 **그 주 전체가 인스턴스 없이 지나간다** — 이 job 이 고치려는 버그가 그대로
> 재현된다. 월요일이 아닌 날은 get-or-create 가 no-op 이라 비용이 없고, 재기동 구멍을 하루
> 안에 자가치유한다.

기동: `main.py` lifespan 이 **`SCHEDULER_ENABLED=true`** 일 때만 `build_scheduler().start()`.
기본 OFF — 테스트/로컬은 안 돈다(데모는 시드로 커버).

> ⚠️ **in-process** — 다중 인스턴스 배포 시 중복 실행(모든 job idempotent → 안전, 단일 인스턴스 권장).
> Render 배포 시 `SCHEDULER_ENABLED=true` + 단일 인스턴스 = #24 PM 배포 설정.
> 미등록 job(anonymize_inactive=#15 등)은
> job 함수 구현 후 `runtime.build_scheduler` 에 add_job 추가.
