# API 변경 기록 (api-change-log)

[`api-contract.md`](api-contract.md) 의 버전별 변경 이력. **최신이 위.**
계약을 바꾸는 PR 은 이 파일에 항목을 추가한다 (AGENTS.md §3).

형식: `## v<버전> — <날짜> (<PR/이슈>)` + 변경 불릿. 호환 깨짐은 ⚠️ 로 표시.

---

## v1.19 — 2026-07-08 (Inbox 보관함 조회·복원 + 승격 대상 구분)

- **버그 픽스**: `GET /inbox` 가 모든 쿼리에 `archived_at IS NULL` 을 하드코딩해 **보관(archived) 항목이 어떤 필터로도 조회 불가**하던 문제 수정. `?status=archived` 로 보관함 조회 가능(기본 목록은 여전히 archived 제외).
- 신규 `POST /inbox/{id}/restore` — 보관 취소(`archived_at` 클리어 + `status`→classified/captured). 활성 항목이면 멱등, 없으면 404 `INBOX_NOT_FOUND`. hard delete 없음.
- `InboxItem` 응답에 **파생 필드 `promotedTo`** 추가: `status=promoted` 일 때 `"goal"`/`"action"`(promotedGoalId 유무로 계산). FE 가 "목표로/할 일로" 배지를 정확히 구분하고 action 딥링크를 걸 수 있게 함. **DB 컬럼·마이그레이션 없음**(순수 파생).
- 신규 에러코드 없음. 테스트: 보관함 조회·복원(멱등)·promotedTo 구분 5종 추가.

## v1.18 — 2026-07-08 (다일 계획 스케줄러 + `scope` + DB 상태 busy 통합, #112)

- `POST /plans/generate` 요청에 `scope`(선택, 기본 `"horizon"`) 추가: `"horizon"`=**마감까지** 전 구간(실행이 마감 전 여러 날에 분배) / `"week"`=`targetDate` 가 속한 **달력 주(월~일)** 만. 미지정 시 `"horizon"` — 기존 동작(마감까지 배치)과 동일해 하위호환
- ⚠️ 배치 동작 변경: 이전에는 `reserve_habit_sessions`(습관 하루 1세션) 재사용으로 모든 카드가 `targetDate` **하루에 백투백**으로 몰렸다. 이제 다일 스케줄러가 **여러 날에 분산**(하루 집중 상한 + 피크 시간대 우선 + 카드 간 휴식 + 긴 카드 세션 분할)
- 어느 scope 든 **이미 승인된 `scheduled_blocks` + 고정 일정(`fixed_schedules`, 수업·알바) + DB `time_policies`(온보딩 후 수정 포함)** 를 모두 busy 로 반영해 그 위에 겹쳐 잡지 않는다(비파괴 fit-around — 사용자가 손댄 일정·완료 카드 보존). **#112 완전 해결**. 캘린더 freebusy 회피는 후속(P1)
- 응답 envelope/필드 불변(`blocks` 시각만 분산). 신규 에러코드 없음

## v1.17 — 2026-07-08 (주간 블록 목표 연결 `goalId` + 카테고리 실분류)

- `GET /plans/weekly` 의 `blocks[]` 와 `PATCH /plans/{planId}/blocks/{blockId}` 응답에 **`goalId`** (`goal_<uuid>` | null) 추가 — 블록이 매달린 `action_item.goal_id` 노출(마이그레이션 없음). FE 가 블록을 목표 분류(집중/유지)·색상과 연결할 수 있게 함. additive — 기존 클라이언트 영향 없음
- 배경: 주간 시간표 블록이 전부 "기타" 로 렌더 — 블록에 목표 연결 정보가 없고 category 도 대부분 `other`(라이브 실측 other 29/study 5/health 4)
- `goal_decompose.v1.md` 프롬프트에 category enum 전체 명시 + "분류 명확하면 other 금지" 규칙 — 신규 생성 계획의 액션/블록 카테고리 실분류 유도
- 계획 승인 시 heaviest goal 의 category 가 `other`(인터뷰 미분류 기본값)면 분해된 액션 카테고리 **다수결로 파생** — 실카테고리가 이미 있으면 불변
- ⚠️ 기존 저장 데이터의 `other` 는 그대로(신규 생성분부터 개선)

## v1.16 — 2026-07-07 (`POST /plans/generate` 빈 본문 자동 복구)

- 빈 본문(`{}`) 시 422 대신 **그 유저의 최근 '정상 종료' 인터뷰 세션(abandoned 제외)으로 자동 복구**해 계획 생성 — FE 가 새로고침/재진입으로 메모리의 sessionId 를 잃으면 온보딩 4/4 주간 계획이 빈 화면이 되던 문제의 서버측 해결
- 완료된 인터뷰가 아예 없으면 기존대로 422 `COMMON_VALIDATION_ERROR`(메시지 개선). `outcome`/`interviewSessionId` 경로는 불변(하위호환)
- `InterviewRepo.get_latest_finished` 추가. §8 표의 낡은 "generate 만 501 스텁" 주석 정정(구현 완료 상태 반영)

## v1.15 — 2026-07-07 (#20 — `POST /reflection/batch` 실구현)

- `POST /reflection/batch` 501 스텁 → **실구현**. S17 저녁 일괄 회고: 미체크(in_progress) 카드들을 한 번에 종결.
- 요청 `{ items: [{ executionId, completionStatus, failureTags?(0~2), memo? }] }`(빈 배열 no-op, 상한 50). 응답 `{ processedCount, taggedCount, needsFailureTags[] }`.
- 각 항목은 `POST /today/check-ins` 와 동일 전이 + failed/partial_done 은 실패 사유 동시 기록. **전량 검증 후 단일 트랜잭션**(하나라도 무효면 전체 롤백). Idempotency-Key 필수(미들웨어). 신규 에러코드 없음(기존 재사용).

## v1.14 — 2026-07-02 (인터뷰 재시작 승리 — staging FE 연동 실측 후속)

- ⚠️ `POST /interview/sessions` 동작 변경: 진행 중 세션이 있으면 **409 `INTERVIEW_SESSION_EXISTS` 대신 그 세션을 `endReason=abandoned` 로 닫고 새 세션 생성(항상 201)** — 재시작 승리(restart-wins)
- 배경: staging FE(Vercel) 연동에서 클라이언트가 sessionId 를 잃으면(새로고침 등) 활성 세션 조회 수단이 없어 409 로 **영구 차단**됨을 실측. `abandoned` 는 DB enum(§5.2)에 이미 존재
- 이어하기 경로 불변: sessionId 보유 시 `next-question` 재개. 동시성 lock(`AGENT_CONCURRENT_ACCESS`) 불변
- `INTERVIEW_SESSION_EXISTS` 에러 코드는 더 이상 발생하지 않음 (enum 은 하위호환 위해 유지)

## v1.13 — 2026-06-23 (#23-D — 톤 prefix LangGraph(interview·first_plan) 배선)

- interview(3)·first_plan(2) LangGraph 노드의 `aiClient.run` 에도 톤 prefix 적용 → **모든 LLM 호출(5도메인 8지점) 톤 적용 완료**
- 전달 = state 가 아닌 **`config["configurable"]["tone_mode"]` 채널**(세션과 동일, ADR-0005 §7.1). `InterviewState`/`FirstPlanState` 스키마 불변
- 노드: `tone_mode=_tone_mode(config)`. 주입: `interview_runner._config(session, tone_mode)` + `planning._config(session, tone_mode)` — runner/route가 `user.tone_mode` 전달
- ⚠️ API/스키마/DB 변경 없음. `test_tone_wiring` +4(노드·runner·없을 때 None). 전체 314 passed
- 이로써 #23(Settings/Privacy/톤모드) 톤 배선 전부 완료

## v1.12 — 2026-06-23 (#23-C — 톤 prefix aiClient.run 배선)

- 톤모드(gentle/strict/encouraging) → LLM 시스템 프롬프트 prefix 1줄을 **단일 게이트에서** 적용
- `aiClient.run()` 에 **선택 kwarg `tone_mode: str | None = None`** 추가(ADR-0003 동결 시그니처 + addendum). 렌더 직후 `compose_system_prompt` 로 prefix 선행. None/미지원 → 기존 동작(회귀 없음)
- 배선 호출처: `inbox`·`recovery` 라우트(`user.tone_mode`) + `morning_brief` cron(`tone_mode` 파라미터)
- **신규 문서** `docs/decisions/0003-llm-tool-executor.md` (그동안 dead link였던 ADR-0003 소급 명문화 + tone addendum)
- ⚠️ **API endpoint/스키마/DB 변경 없음** (LLM 게이트 내부 + 프롬프트 합성만). interview·first_plan(LangGraph state)은 후속 슬라이스
- `test_tone_wiring` 4건(prefix 적용/미적용/미지원값/cron 전달)

## v1.11 — 2026-06-23 (#23-B — Privacy: consent + 즉시 익명화)

- Settings/Privacy(§16) S28 실구현 — `GET/POST /privacy/consent` + `POST /settings/anonymize` (501 스텁 → 실 endpoint)
- ⚠️ **새 테이블/마이그레이션** `user_consents` (Alembic `c2d3e4f5a6b7`, append-only) — AGENTS §8 팀 합의 후 머지. consent_type(`required`/`marketing`/`research`) × `is_granted`
- `GET /privacy/consent` — consent_type 별 최신 1행. `POST` `{consentType, granted}` — append-only INSERT 후 갱신 현황 반환. 잘못된 type 422 `COMMON_VALIDATION_ERROR`
- `POST /settings/anonymize` — **2단계 확인 토큰**(HMAC, 5분). 토큰 없으면 발급(`confirmation_required`), 동봉 재요청 시 `_encrypted` 7종 + 이름 `[anonymized]` 마스킹 + `is_anonymized`/`anonymized_at` set(`anonymized`). hard delete 아님
- 새 에러코드 `PRIVACY_INVALID_CONFIRMATION`(422) · `PRIVACY_ALREADY_ANONYMIZED`(409). 신설 `auth/confirm.py`(확인 토큰), `repositories/{consent,privacy}_repo.py`
- 톤 prefix 의 `aiClient.run()` 배선은 후속(ADR-0003 addendum) — 범위 아님

## v1.10 — 2026-06-22 (#21-C — Habit Penalty)

- Reviews(§13) S22 실구현 — `GET /reviews/habit-penalty` + `POST /reviews/habit-penalty/{habitId}/accept`
- 감지: 직전 완료 주 기준 최근 3주 연속 `done_count < target_count*0.5`. 순수 함수 `orchestrator/habit_penalty.py`. suggestedFrequency = 3주 평균(round, 최소 1, 현재보다 작게)
- `GET` 후보 — 이번 사이클 이미 결정한 habit(`last_penalty_evaluated_at` ≥ 직전 완료 주) 제외 (비난 아닌 재설계 톤)
- `POST accept` — Idempotency-Key 필수(§1.7 미들웨어가 경로 강제). 수락 시 frequency=target=suggested + `last_penalty_decision='accepted'`. 조건 미충족/중복 422 `HABIT_PENALTY_NOT_ELIGIBLE`
- 새 에러 코드 `HABIT_PENALTY_NOT_ELIGIBLE`. `habit_instance_repo.list_recent_for_habit` + `habit_repo.apply_penalty` 추가. DB 마이그레이션 없음
- reject(+4주 cooldown) 경로는 후속 — 이로써 #21 (Weekly Plan+Review) 전체 완료(#21-A/B/C)

## v1.9 — 2026-06-22 (#21-B — Weekly Plan + 직접 편집)

- Planning(§8) S14/S15 실구현 — `GET /plans/weekly` + `PATCH /plans/{planId}/blocks/{blockId}`
- `GET /plans/weekly?weekStart=` — 그 주 월요일로 정규화(생략 시 이번 주). 7일 × `blocks[]`(blockId/actionId/title/category/startAt/endAt/blockStatus/source). 영속 `scheduled_blocks` ⨝ `action_items`
- `PATCH .../blocks/{blockId}` — `{startAt, endAt?}`, **15분 snap**, endAt 생략 시 길이 보존. 적용 시 `source='user_edit'`. Plan 테이블 없음 → planId 는 주 논리 식별자, 편집 권한은 blockId
- 새 에러 코드: `PLAN_BLOCK_NOT_FOUND`(404) · `PLAN_BLOCK_CONFLICT`(422) · `PLAN_INVALID_TIME`(422) · `POLICY_VIOLATION`(422)
- 정책 위반 판정 = 순수 함수 `orchestrator/plan_edit.py`(sleep/lunch/late_night_block 윈도우, 자정 wrap·카테고리 게이팅). `no_touch`/`break_min`/freebusy·fixed_schedule 충돌은 후속
- 신설 `repositories/scheduled_block_repo.py`. ⚠️ DB 마이그레이션 없음(기존 `scheduled_blocks`)
- S22 habit-penalty 는 #21-C 잔여

## v1.8 — 2026-06-22 (#21-A — Weekly Review)

- Reviews(§13) S21 실구현 — `GET /reviews/weekly` + `POST /reviews/weekly/generate` (501 스텁 → 실 endpoint). **룰 기반**(LLM 한 줄 평 P2)
- `GET /reviews/weekly?weekStart=` — precomputed `period_summaries`(weekly) 우선, 없으면 **즉석 계산(쓰기 X)**. `weekStart` 는 그 주 월요일로 정규화, 생략 시 이번 주
- `POST /reviews/weekly/generate` — 강제 재집계 + 영속화(덮어쓰기, 디버그)
- 신설 새 에러 코드 `REVIEW_INVALID_WEEK`(422, weekStart 형식 오류)
- 집계: 순수 함수 `orchestrator/weekly_review.py`(`compute_weekly_kpis`) — adherence/consistency/resilience/category/peak·drain window/one-liner. `restartSuccessRate`·`repeatedFailureCount`·`policyUpdateCandidates` 는 #21-A 에서 null/[] (후속)
- `resilienceRate` = 실패 중 회복 카드 **수락** 비율(#21-A 정의). "24h 내 완료" 정밀화는 #20-B 후
- 신설 `repositories/review_repo.py`, `scheduler/weekly_review_precompute.py`(일요일 03:00 cron job, idempotent). 시각 트리거 등록은 #24
- ⚠️ DB 마이그레이션 없음 (기존 `period_summaries` 테이블 사용). S22 habit-penalty · S14/S15 weekly plan 은 #21-B/#21-C

## v1.7 — 2026-06-22 (#62 / 9-C — First Plan SAVING 전체 영속화 + Draft 영속화)

- ⚠️ **새 테이블/마이그레이션** `plan_drafts` (Alembic `b1f2a3c4d5e6`) — AGENTS §8 팀 합의 후 머지. First Plan Draft 영속화(payload JSONB 스냅샷 + status/expires_at).
- `POST /plans/generate` — Draft 를 `plan_drafts`(72h 만료)에 저장하고 **실제 `planId`(UUID) 반환**(이전 ephemeral `plan_…` → 변경).
- `GET /plans/{planId}` 실구현 — 저장된 Draft 미리보기 재구성(LLM 0회). 없으면 404 `PLAN_DRAFT_NOT_FOUND`.
- ⚠️ **FE 계약 변경** `POST /plans/{planId}/approve` — body 재전송 방식 → **`planId` 로 Draft 로드** 방식으로 전환(body 불필요). goals/goal_nodes/action_items/scheduled_blocks 단일 트랜잭션 영속화(temp_uuid→실 UUID, goal_node 트리 parent 링크, action_item.goal_id/goal_node_id) + **최대 3회 재시도**(ADR-0005 §2.5.1). 만료 410 `PLAN_DRAFT_EXPIRED`, 이미 승인 시 멱등.
- 신규 에러코드: `PLAN_DRAFT_NOT_FOUND`(404), `PLAN_DRAFT_EXPIRED`(410).
- 72h Draft 만료 cron `run_expire_stale_drafts`(`scheduler/expire_drafts.py`, idempotent) + scheduler README 시간표 갱신. 트리거 등록은 #24.
- ⚠️ **제외**: `dependency_links` 영속화 — `GoalDecomposition` 에 의존성 소스 데이터가 없어 LLM 스키마 확장이 선행 필요 → 별도 후속.

## v1.6 — 2026-06-22 (#32 / 9-B — Planning LLM 통합 / First Plan)

- Planning(§8) `POST /plans/generate` + `POST /plans/{planId}/approve` 실구현 (501 스텁 → First Plan orchestrator). ADR-0005 §2.5.1 Sequential + 룰 fallback.
- 입력: `outcome`(InterviewOutcome 인라인) 우선, 없으면 `interviewSessionId` 로 종료 세션 slot 결정적 투영(LLM 0회). `targetDate` 미지정 시 오늘(KST). 둘 다 없으면 422 `COMMON_VALIDATION_ERROR`, 잘못된 세션 id 는 404 `INTERVIEW_SESSION_NOT_FOUND`.
- 흐름: VALIDATING(**Focus≤3 / Maintain≤5** 게이트, LLM 0회) → decompose(`planning/goal_decompose` LLM, 8s timeout→룰) → schedule(`goal_structuring.py` 룰 스케줄러, LLM 0회) → review(`planning/plan_quality` LLM, 8s timeout→룰). 한도 초과 시 **LLM 분해 전** 422 `GOAL_TIER_LIMIT_EXCEEDED`.
- 응답 `FirstPlanResponse` — 항상 `isDraft=true`(AGENTS §1.4). `aiSource`=`llm`|`rule`(orchestrator `used_fallback`). `blocks`=룰 스케줄러가 action_item 을 가용 시간(free/busy)에 배치한 미리보기(KST). 배치 실패 항목은 `warnings`.
- `POST /plans/{planId}/approve` (HITL [수락]) — `FirstPlanApproveRequest`(outcome + action_items + blocks 되돌려 전달, `planId` 는 ephemeral echo). `policy_guarded_transaction`(PR #30 재사용) 단일 트랜잭션으로 action_items + scheduled_blocks 영속화. 절대 시간 정책(수면/노터치) 위반 시 롤백 + 422 `PLAN_POLICY_VIOLATION`, 그 외 영속화 실패는 롤백 + 500 `PLAN_SAVE_FAILED`. 응답 `is_draft=false`(명시 승인, ADR-0005 §7.2).
- 온보딩 전이 — approve 가 `ONBOARDING_FIRST_PLAN → ONBOARDING_NOTIFICATIONS` 전이(멱등)를 수행. Issue #17 이 이 전이를 "#9(First Plan) 다음에" 로 First Plan 에 위임했고(각 도메인 라우터가 자기 단계 완료 시 전이), 그동안 빠져 있어 온보딩 체인이 `ACTIVE` 에 도달하지 못하던 갭을 메움. api-contract §3 표 갱신.
- 동시성 lock(ADR-0005 §7.6) — `user_id × planning` advisory lock, 다중 디바이스 동시 생성/승인 시 409 `AGENT_CONCURRENT_ACCESS`.
- 새 에러 코드: `PLAN_POLICY_VIOLATION`(422), `PLAN_SAVE_FAILED`(500). DB 마이그레이션 없음(기존 `action_items`·`scheduled_blocks` 모델 사용).
- 금지어 후처리 / `llm_runs` 로깅 / 8s timeout 룰 fallback 은 LLM 게이트(`aiClient.run`, Issue #5)가 일관 적용 — 강제 timeout·llm_runs 2행 기록 회귀 테스트 추가.
- ⚠️ **설계 변경 주의**: 이슈 9-B 본문은 "LLM 4회(Validation·Planning②③·Review) + `validation/planning/review.v1.md` 3종"을 기술하나, **ADR-0005(Accepted)가 LangGraph 2-LLM(decompose·review) + 룰 Validation/Scheduler 설계로 대체**. 본 구현은 ADR-0005 를 따른다. 미반영: goal/goal_node 트리 + dependency_links 영속화(후속 SAVING), `planId` 영속 draft 테이블.

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
