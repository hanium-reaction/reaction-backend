# API 변경 기록 (api-change-log)

[`api-contract.md`](api-contract.md) 의 버전별 변경 이력. **최신이 위.**
계약을 바꾸는 PR 은 이 파일에 항목을 추가한다 (AGENTS.md §3).

형식: `## v<버전> — <날짜> (<PR/이슈>)` + 변경 불릿. 호환 깨짐은 ⚠️ 로 표시.

---

## v1.5 — 2026-06-04 (#23-A — Settings 코어)

- Settings(§16) S23 실구현 — `GET /settings` + `PATCH /settings/tone-mode` (501 스텁 → 실 endpoint)
- `GET /settings` = `toneMode`(User) + `language`(="ko" 잠금) + `timezone`(User) + `notifications` 요약(§15 `get_by_user`, 미설정 시 null). **읽기 전용 — 알림 행 미생성**(notifications GET 의 get_or_create 와 구분)
- `PATCH /settings/tone-mode` — `gentle`/`strict`/`encouraging` Pydantic Literal 검증, 그 외 422 `COMMON_VALIDATION_ERROR`. onboarding 상태 전이 없음. `user_repo.set_tone_mode` 헬퍼 추가
- 신설 `llm/prompt_compose.py` — 톤 prefix 1줄 분기 **순수 헬퍼**(테스트됨). ⚠️ `aiClient.run()` 배선은 **미포함** — ADR-0003 §1 동결 시그니처 변경 + LangGraph state(tone_mode) 전달 수반 → 후속 PR(ADR-0003 addendum)
- `/privacy` 라우터 신설 + `main.py` 등록. ⚠️ **S28 Privacy(anonymize·consent)는 #23-B** — `POST /settings/anonymize`·`GET/POST /privacy/consent` 는 501 `COMMON_NOT_IMPLEMENTED` 스텁. consent 저장은 append-only `user_consents` 테이블(마이그레이션 동반) 예정
- 새 에러 코드 없음 / DB 마이그레이션 없음 (기존 `users` 컬럼만 사용)

## v1.4 — 2026-06-02 (#19-C — cron job 로직)

- `scheduler/morning_brief.py` — `run_morning_brief_for_user` job: 룰 헤드라인 + `aiClient.run("brief/morning_brief")` fallback → `daily_briefs` INSERT. **idempotent**(같은 user+date 이미 있으면 skip). big_rock = priority 최상위 카드
- `scheduler/interruption_resolver.py` — `run_interruption_resolver` job: `resumed_after_interrupt IS NULL` & 6h 경과 → `false`. idempotent (NULL 만 대상)
- 신설 `interruption_event_repo`. `daily_brief_repo` 에 `create` 추가
- `MorningBriefDraft` LLM Structured Output schema (`schemas/today.py`)
- ⚠️ **API endpoint 변경 없음** (cron 내부 로직). 스케줄 트리거(매일 06:00 / 6h)는 **#24 운영준비**에서 APScheduler 등록 — 본 PR 은 job 함수 + 테스트만
- scheduler README 에 구현 상태 표 추가

## v1.3 — 2026-06-01 (#19-A — Today 조회)

- Today(§10) 조회 2 endpoint 실구현 — `GET /today/agenda` + `GET /today/actions/{id}` (조회 전용, 쓰기 없음)
- `agenda` = KST 오늘 기준 `brief`(daily_briefs) + `cards`(action_items target_date) + `habits`(이번 주 instance) + `fixedSchedules`(오늘 요일)
- 신설 `daily_brief_repo`(get_by_date). `action_item_repo` 에 `list_by_date`·`get_by_id` 추가 (status 변경 메서드는 추가 X — 원본 status 보존, AGENTS.md §2)
- ⚠️ Focus 실행 로깅(start/pause/resume/check-ins)은 **#19-B** 로 분리 — `execution_events.scheduled_block_id` NOT NULL → First Plan(#18/#32) scheduled_blocks 의존
- ⚠️ Morning Brief 생성 cron(daily_briefs INSERT, 룰+LLM)은 **#19-C** — 본 PR 은 조회만. brief 없으면 `agenda.brief=null`
- `agenda.habits[].title` 은 현재 빈 문자열 (habit 본체 join 은 FE 또는 후속) — 진행 카운트(target/done)만 제공

## v1.2 — 2026-05-31 (#6 — Deep Interview 실배선)

- Interview(§4) — mock 스텁 → **LangGraph 인터뷰 엔진 + DB 영속화** 연결
- `POST /interview/sessions` — 실제 세션 생성(`interview_sessions` 행) + FSM 첫 질문 (LLM 1회, 룰 fallback)
- `POST /interview/sessions/{id}/answers` — 답 채점·정규화·UPSERT(`interview_slot_answers`) 후 다음 질문, 종료 시 요약+outcome
- `POST /interview/sessions/{id}/finish` — 조기 종료(`early_user`) + outcome 빌드
- `sessionId` 는 이제 **UUID**(과거 고정 `interview_demo_0001` 폐기)
- ⚠️ `ambiguityScore`(int) 의미 = **남은 미해결 필수 슬롯 수**(진행될수록 감소)
- `GET /interview/slot-catalog` 항목에 **`options`**(chip/select 보기) 추가. text/date/range 는 빈 배열
- `InterviewSession` 응답에 종료 턴 한정 **`summary`**(S03 확인 카드) + **`outcome`**(First Plan 시드) 추가 (진행 중엔 null)
- ⚠️ 미해결(후속): 단일 활성 세션 enforce·동시성 lock(ADR-0005 §7.6)·재조립 시 transient 상태 리셋

## v1.1 — 2026-05-27 (#22 part 2 — Inbox)

- Inbox(§18) — mock → **실 DB 구현** + AI 분류 + Triage 변환
- `POST /inbox` — `aiClient.run("inbox/classify")` Sequential Parser (Tool Executor 단일 게이트, ADR-0003 / ADR-0005 §4 단계 5)
  - LLM 실패 시 룰 fallback (키워드 매칭, `confidence=0`, `needsUserOverride=true`)
  - `raw_text` AES-256-GCM 암호화 저장 (`raw_text_encrypted`, `safety.encrypt_inbox_text` 새 헬퍼)
  - 응답 시 ai_category_guess 채워지면 `status=classified` 자동
- `POST /inbox/{id}/convert-to-goal` — Goal 생성 + Maintain 한도 enforce + inbox `status=promoted`
- `POST /inbox/{id}/convert-to-action` — ActionItem(`source=inbox`) 생성 + inbox `status=promoted`
- `POST /inbox/{id}/archive` — soft delete (`status=archived`)
- ⚠️ **endpoint 변경** — 기존 `POST /inbox/{id}/promote` → `POST /convert-to-goal` 로 rename, `DELETE /inbox/{id}` → `POST /archive` 로 rename
- 신설 repo: `inbox_repo` + `action_item_repo` (후자는 `create_from_inbox` 단일 진입점; 본격 CRUD 는 Issue #19/#20 후속)
- `userCategory` 6종 enum 강제 (`study/project/health/routine/schedule/other`)
- ⚠️ `aiSource` / `isDraft` 등 LLM 메타 응답 노출 (ADR-0005 §7.2 권장) 은 본 PR 미포함 — 후속 chore PR 로 분리

## v1.0 — 2026-05-26 (#22 part 1 — Goals + Habits)

- Goals(§6) · Habits(§7) — mock → **실 DB 구현**
- Goals tier 한도 enforce — Focus ≤ 3 / Maintain ≤ 5 (422 `GOAL_TIER_LIMIT_EXCEEDED`, ADR-0005 §2.5.1). Parked 자유
- Goals `goalId` prefix `goal_<uuid>`, `categoryEnum` 검증 (9종), tier 변경 시 한도 재검사, soft delete (`archived_at` + `status=archived`)
- Goals `POST /goals/{id}/decompose` — mock 룰 stub 유지 (LLM 통합은 PR #33 인프라 + ADR-0005 §4 단계 5 후속 PR)
- Habits CRUD — `habitId` prefix `habit_<uuid>`, `frequencyPerWeek` 1~7 (Pydantic + DB CheckConstraint), 빈도 변경 시 `target_count` 동기화
- `POST /habits` 시 이번 주 `habit_instances` **자동 생성** (cron 도입 전 임시; Issue #24 cron 후속)
- Habit instances — `GET /habit-instances?weekStart=YYYY-MM-DD` (누락 시 KST 월요일), `POST /habit-instances/{id}/check` done_count++. `instanceId` prefix `hinst_<uuid>`
- 신설 repo 3개: `goal_repo` · `habit_repo` · `habit_instance_repo`
- 에러 코드 추가: `GOAL_TIER_LIMIT_EXCEEDED` (`GOAL_FOCUS_LIMIT` / `GOAL_MAINTAIN_LIMIT` 은 deprecated, enum 잔존)
- ⚠️ **Inbox(§18) 는 본 PR 미포함** — PR #33 (LLM Infra) 머지된 main 위에서 후속 PR (`#22-B`) — `aiClient.run("inbox/classify.v1")` 통합 + `safety/encryption` 으로 `raw_text_encrypted` 실 암호화 + `convert-to-action`/`convert-to-goal`/`archive` 새 endpoint

## v0.9 — 2026-05-25 (#17)

- Time Policies(§5) · Fixed Schedules(§19) · Notifications settings(§15) — mock → **실 DB 구현**
- §3 Onboarding `users.onboarding_state` 자동 전이 트리거 표 추가:
  - `POST /fixed-schedules` → CALENDAR/MANUAL_SCHEDULE → POLICIES
  - `POST /time-policies` → POLICIES → FIRST_PLAN
  - `PATCH /notifications/settings` → NOTIFICATIONS → ACTIVE
  - 각 트리거 멱등 (이미 더 진행된 상태면 no-op)
- §5 `POST /time-policies/prefill-from-interview` — `InterviewSlotAnswer` 룰 매칭 + default 후보. 응답 `policyId` 는 prefill 임시 ID(`policy_prefill_N`), DB 미저장
- §9 Calendar `/connect` (POST/DELETE) → **501 P1** (PM Alpha MVP 결정). freebusy / sync-preview / approve-insert 는 Issue #18 까지 mock 유지
- §15 Notifications `/settings` (GET/PATCH) — `notification_settings` 테이블, 사용자당 1행, `get_or_create` 패턴. subscribe/unsubscribe 는 Issue #25 (PWA) 까지 mock
- §19 Fixed Schedules CRUD — `days_of_week` 검증 (mon/tue/wed/thu/fri/sat/sun), `start < end` 검증
- 신설 repo 3개: `time_policy_repo` · `fixed_schedule_repo` · `notification_repo`. `user_repo.advance_onboarding` 헬퍼 추가

## v0.8 — 2026-05-23 (#16)

- Auth(§2) 실구현 — Google id_token 검증(`google-auth`) + 자체 JWT(HS256, access 60m / refresh 14d) 발급, refresh 회전 X
- `/auth/logout` — refresh `jti` revoke set 등록 (in-memory MVP, 추후 DB 테이블 이전)
- 인증 미들웨어 신설 (`api/deps.get_current_user`) — health/auth/onboarding 외 15개 라우터에 router-level `Depends` 적용
- §3 Onboarding `/status` 실구현 — `CurrentUser.onboarding_state` 기반 → `suggestedNextScreen` 매핑 표 추가
- 에러 코드 추가: `AUTH_TOKEN_EXPIRED` (만료 vs 무효 분기)
- CORS — `cors_allow_origin_regex` 옵션 추가 (Vercel preview URL 패턴 매칭)
- 환경변수 추가: `GOOGLE_OAUTH_CLIENT_ID/_SECRET`, `JWT_SECRET`, `JWT_ALGORITHM`, `JWT_ACCESS_TOKEN_TTL_MINUTES`, `JWT_REFRESH_TOKEN_TTL_DAYS`, `AUTH_STUB_MODE`
- ⚠️ Google OAuth client_id 는 PM 발급 대기 — 로컬은 `AUTH_STUB_MODE=true` 로 우회 가능 (고정 demo 클레임)

## v0.7 — 2026-05-23 (#3-D, partially addresses #3)

- Goals(§6)·Habits(§7)·Inbox(§18) mock/stub 응답 구현 — 17 endpoint
- `inbox` 라우터 신설 + `main.py`·`api/routes/__init__.py` 등록 (도메인 라우터 18개)
- `habits.py` 에 두 라우터 export — `router(/habits)` + `router_instances(/habit-instances)`
- 에러 코드 추가: `GOAL_NOT_FOUND`·`GOAL_FOCUS_LIMIT`·`GOAL_MAINTAIN_LIMIT`, `HABIT_NOT_FOUND`, `INBOX_NOT_FOUND`·`INBOX_ALREADY_PROMOTED`
- §6 Goals decompose 응답에 `parentId` 필드 (예시 일치)
- Person 1 arc 완료 (#3-B·C·D) — 18개 도메인 라우터 중 11개(health 포함) 구현 완료

## v0.6 — 2026-05-22 (#3-C, partially addresses #3)

- Time Policies(§5)·Calendar(§9)·Notifications(§15)·Fixed Schedules(§19) mock/stub 응답 구현 — 18 endpoint
- `fixed_schedules` 라우터 신설 + `main.py` 등록 (도메인 라우터 17개)
- 에러 코드 추가: `POLICY_NOT_FOUND` · `CALENDAR_NOT_CONNECTED` · `CALENDAR_CONFLICT` · `FIXED_SCHEDULE_NOT_FOUND` · `FIXED_SCHEDULE_OVERLAP` · `NOTIF_TIME_RANGE`
- Notifications PATCH 시간 범위 검증 — 모닝 06~10시·저녁 19~23시 위반 시 `NOTIF_TIME_RANGE`

## v0.5 — 2026-05-22 (#3-B, partially addresses #3)

- Auth(§2)·Onboarding(§3)·Interview(§4) mock/stub 응답 구현 — 11 endpoint
- 도메인 응답 스키마 camelCase 확정 — `CamelModel` 베이스 추가 (api-contract §1.9)
- §4 `slot-catalog` 필드명 정정: `id → slotKey`, `type → answerType`
- 에러 코드 추가: `AUTH_INVALID_TOKEN`·`AUTH_INVALID_ID_TOKEN`·`INTERVIEW_SESSION_EXISTS`·`INTERVIEW_SESSION_NOT_FOUND`·`INTERVIEW_SLOT_LOCKED`
- ⚠️ 슬롯 카탈로그 — DevBaseline §6.2.2 원문 표 20행 vs 요약 "19" 불일치. 현재 20 슬롯으로 구현, 베타 전 PM 재검토 필요.

## v0.4 — 2026-05-22 (#3-A, partially addresses #3)

- [ADR-0002](decisions/0002-api-contract-freeze.md) 로 응답 계약 **동결**:
  envelope-less 성공 응답 · `ErrorResponse` 단일 에러 형태 · Idempotency-Key · UTC 저장/KST(+09:00) 응답
- `/inbox` (S24 Life Inbox) 섹션 추가 — 계약 갭 보강 (§18)
- `/fixed-schedules` (S05 Manual Fixed Schedule) 섹션 추가 — 계약 갭 보강 (§19)
- §1.4 에러 prefix 표에 `INBOX_*` · `FIXED_SCHEDULE_*` · `COMMON_*` 추가
- §1.9 필드 네이밍 규약 명시 (도메인 객체 camelCase)
- 입력 검증 실패(422)도 `ErrorResponse` 로 통일 — `code: COMMON_VALIDATION_ERROR`

> ⚠️ Issue #3 본문의 `ApiResponse<T>` envelope 스니펫은 stale — 본 버전(envelope-less)이 정본.
> ADR-0002 §2.5 참고. 본문 정정은 PM.

## v0.3 — 2026-05-21 (#1 walking skeleton)

- 최초 계약 — 16 도메인, envelope-less 성공 응답, `ErrorResponse` 에러 형태
- 이전 `swagger.yaml` v0.2.0 폐기
