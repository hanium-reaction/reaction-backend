# re:action API Contract v0.7

> 진실 소스. 모든 endpoint 변경은 이 문서 PR과 동반된다.
> 기준 문서: `Reaction_DB_설계서_v0.7.1` + `Reaction_DevBaseline_v1.0_2026-05-15`
> 응답·에러·Idempotency·시간 규약은 [ADR-0002](decisions/0002-api-contract-freeze.md) 로 동결됨.
> 변경 이력은 [`api-change-log.md`](api-change-log.md). 이전 swagger.yaml v0.2.0은 **폐기**.

---

## 1. 응답 규약

### 1.1 base URL

| 환경 | URL |
| --- | --- |
| local | `http://localhost:8000` |
| compose | `http://reaction-backend:8000` |
| staging | TBD |
| production | TBD |

### 1.2 성공 응답 형태

성공 응답은 **envelope 없이 도메인 객체를 직접** 반환한다 (OpenAPI 친화 + 클라이언트 단순).

```json
{ "goalId": "goal_abc", "title": "캡스톤", "tier": "FOCUS", ... }
```

### 1.3 에러 응답 형태 (4xx / 5xx)

```json
{
  "code": "INTERVIEW_SLOT_LOCKED",
  "message": "이미 종료된 세션의 슬롯은 수정할 수 없어요.",
  "field": null,
  "server_time": "2026-05-21T01:23:45.678+09:00"
}
```

- `code` — 도메인 prefix UPPER_SNAKE_CASE
- 표준 HTTP status code 매핑: 400 / 401 / 403 / 404 / 409 / 422 / 500

### 1.4 에러 코드 도메인 prefix

| prefix | 도메인 |
| --- | --- |
| `AUTH_*` | 인증/세션 |
| `USER_*` | 사용자 |
| `ONBOARDING_*` | 온보딩 상태머신 |
| `INTERVIEW_*` | 딥 인터뷰 |
| `POLICY_*` | 시간 정책 / 정책 스냅샷 |
| `GOAL_*` / `HABIT_*` | 목표/습관 |
| `PLAN_*` | 계획 생성/편집 |
| `CALENDAR_*` | Google Calendar |
| `EXEC_*` | 실행/체크인 |
| `REFLECT_*` | 회고 |
| `RECOVERY_*` | 회복 옵션 |
| `REVIEW_*` | 주간 리뷰 |
| `NOTIF_*` | 알림 |
| `INBOX_*` | Life Inbox |
| `FIXED_SCHEDULE_*` | 고정 일정 |
| `LLM_*` | LLM 호출 (timeout, fallback used 등) |
| `AGENT_*` | Agent 동시성 (advisory lock 미획득 등, ADR-0005 §7.6) |
| `IDEMPOTENCY_*` | 멱등 키 충돌 |
| `COMMON_*` | 공통 (검증 실패·미구현·내부 오류) |

### 1.5 시간 / 타임존

- 응답 시간 필드는 **KST(+09:00) ISO 8601 with offset**
- 날짜만은 `YYYY-MM-DD` (`target_date` 등)
- 서버 내부 저장은 UTC

### 1.6 인증

- Google OAuth 후 자체 JWT (`Authorization: Bearer <access_token>`)
- access TTL: 60분, refresh TTL: 14일 (default, 후속 결정 가능)
- `AUTH_INVALID_TOKEN` / `AUTH_TOKEN_EXPIRED` 로 401 분기

### 1.7 Idempotency

다음 endpoint는 **`Idempotency-Key` 헤더 필수** (24h 보장):

- `POST /reflection/batch`
- `POST /recovery/decisions`
- `POST /replan/{execution_id}/approve`
- `POST /calendar/events/approve-insert`
- `POST /reviews/habit-penalty/{habit_id}/accept`

같은 key 재호출 → 캐시된 응답 그대로. `IDEMPOTENCY_KEY_MISMATCH` 시 409.

### 1.8 ID 표기

- 문자열, 도메인 prefix 권장: `user_*`, `goal_*`, `action_*`, `block_*`, `exec_*`, `interview_*`, `recovery_*`, `policy_*`, `inbox_*` …

### 1.9 필드 네이밍

- 응답 도메인 객체 필드는 **camelCase** (`goalId`, `ambiguityScore`, `weekStart` …)
- `ErrorResponse`(§1.3) · `HealthResponse`(§17) 등 공통 메타 응답은 정의된 필드명을 그대로 사용 (`server_time` 등)

---

## 2. Auth (`/auth`)

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/auth/google` | Google id_token → 자체 JWT (access+refresh) 발급 |
| POST | `/auth/refresh` | refresh → 새 access |
| POST | `/auth/logout` | refresh 무효화 |
| GET | `/auth/me` | 현재 사용자 (`onboarding_state` 포함) |

---

## 3. Onboarding (`/onboarding`)

상태머신:
```
WELCOME → ONBOARDING_INTERVIEW → ONBOARDING_CONFIRM
       → ONBOARDING_CALENDAR ⇄ ONBOARDING_MANUAL_SCHEDULE
       → ONBOARDING_POLICIES → ONBOARDING_FIRST_PLAN
       → ONBOARDING_NOTIFICATIONS → ACTIVE
```

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/onboarding/status` | `{ currentState, suggestedNextScreen }` |

`suggestedNextScreen` 매핑 (DevBaseline §5 화면 흐름):

| `onboarding_state` | 다음 화면 |
| --- | --- |
| WELCOME · ONBOARDING_INTERVIEW | S02 |
| ONBOARDING_CONFIRM | S03 |
| ONBOARDING_CALENDAR | S04 |
| ONBOARDING_MANUAL_SCHEDULE | S05 |
| ONBOARDING_POLICIES | S07 |
| ONBOARDING_FIRST_PLAN | S06 |
| ONBOARDING_NOTIFICATIONS | S08 |
| ACTIVE | S10 |

진행 자체는 각 도메인 라우터가 자기 단계 완료 시 `users.onboarding_state` 를 전이.

`users.onboarding_state` 자동 전이 트리거 (Issue #17 실구현):

| 트리거 endpoint | from | to |
| --- | --- | --- |
| `POST /fixed-schedules` | `ONBOARDING_CALENDAR` / `ONBOARDING_MANUAL_SCHEDULE` | `ONBOARDING_POLICIES` |
| `POST /time-policies` | `ONBOARDING_POLICIES` | `ONBOARDING_FIRST_PLAN` |
| `POST /plans/{planId}/approve` | 온보딩 단계 전체 (`WELCOME` … `ONBOARDING_NOTIFICATIONS`) | `ACTIVE` |
| `PATCH /notifications/settings` | `ONBOARDING_NOTIFICATIONS` | `ACTIVE` |

각 트리거는 `expected_from` 에 해당할 때만 전이 (멱등). 이미 더 진행된 상태(예: `ACTIVE`)면 no-op — 같은 endpoint 두 번 호출해도 안전. **첫 계획 승인(`POST /plans/{planId}/approve`)은 온보딩 완료 신호로 보고 어느 온보딩 단계에서든 곧바로 `ACTIVE` 로 마감한다** — 실제 FE 흐름에서 상류 단계 전이(`WELCOME`→…)가 항상 트리거되지 않아 `onboarding_state` 가 `WELCOME` 에 고정되면, 완료 후에도 새로고침 시 재-온보딩되고 계획이 중복 누적되던 문제를 막기 위함(원설계는 `ONBOARDING_FIRST_PLAN → ONBOARDING_NOTIFICATIONS`, #32/Issue #17).

---

## 4. Interview (`/interview`) — S02 딥 인터뷰

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/interview/sessions` | 신규 세션 + FSM 첫 질문. `sessionId` 는 UUID |
| GET | `/interview/sessions/{id}` | 진행 상태 — `ambiguityScore`, `totalTurns`, `currentQuestion`. 종료 세션이면 `outcome` 동봉 |
| POST | `/interview/sessions/{id}/answers` | 슬롯 답 UPSERT — `{ slotKey, value, clientTurn }`. 종료 시 `summary`+`outcome` |
| POST | `/interview/sessions/{id}/next-question` | 현재 슬롯 질문 재생성 (resume용, LLM 호출) |
| POST | `/interview/sessions/{id}/finish` | 조기 종료 `[충분해요]` → `endReason=early_user` + `outcome` |
| GET | `/interview/slot-catalog` | 슬롯 카탈로그 — `slotKey·label·answerType·isRequired·category·options` |

응답 예: `GET /interview/sessions/{id}`
```json
{
  "sessionId": "interview_01",
  "ambiguityScore": 3,
  "totalTurns": 5,
  "endReason": null,
  "currentQuestion": {
    "slotKey": "goals.deadlines",
    "text": "마감일이 정해진 게 있어요?",
    "answerType": "date_picker",
    "options": []
  },
  "summary": null,
  "outcome": null
}
```

- `ambiguityScore`(int) = **남은 미해결 필수 슬롯 수** (진행될수록 감소, 0 이면 충분).
- `currentQuestion.options` = chip/select 보기 (카탈로그 기반). `goals.heaviest` 는 `goals.list` 응답에서 동적 생성. text/date/range 는 `[]`.
- 종료 턴(`endReason` 채워지고 `currentQuestion=null`)에만 `summary`(S03 확인 카드) + `outcome`(First Plan 시드, `InterviewOutcome`)이 채워진다.
- 단일 활성 세션 + **재시작 승리(restart-wins)**: `POST /interview/sessions` 는 진행 중(`endReason=null`) 세션이 있으면 그 세션을 `endReason=abandoned` 로 닫고 새 세션을 만든다 — **항상 201**. 이어하기는 저장해 둔 sessionId 로 `next-question` 재개. (v1.12 이전의 409 `INTERVIEW_SESSION_EXISTS` 는 클라이언트가 sessionId 를 잃으면 복구 불가라 폐기 — 코드 자체는 하위호환 위해 enum 에 유지.)
- 동시성 lock(ADR-0005 §7.6): 모든 mutating 진입점(`sessions`·`answers`·`next-question`·`finish`)은 `user_id × interview` advisory lock 으로 보호. 다른 디바이스가 점유 중이면 409 `AGENT_CONCURRENT_ACCESS` 즉시 fail.
- 구현 상태(#6): 엔진+영속화 배선 + 단일 활성 세션(restart-wins) + 동시성 lock 완료. **후속**: 재조립 시 transient 상태(stall_count·used_fallback) 영속.

---

## 5. Time Policies (`/time-policies`) — S07

`policy_type` 별 discriminated payload.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/time-policies` | 내 활성 정책 전체 |
| POST | `/time-policies` | 신규 정책. payload는 type별 다름 |
| POST | `/time-policies/prefill-from-interview` | S07 진입 시 인터뷰 답 → 정책 prefill |
| PATCH | `/time-policies/{id}` | 부분 수정 |
| DELETE | `/time-policies/{id}` | soft delete (`is_active=false`) |

`policy_type`: `sleep` (1개 필수) / `lunch` / `break_min` / `no_touch` / `late_night_block` / `custom`

---

## 6. Goals (`/goals`) — S26

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/goals` | tier별 그룹 (`focus`/`maintain`/`parked`) |
| POST | `/goals` | 신규. Focus ≤ 3 / Maintain ≤ 5 (초과 시 422 `GOAL_TIER_LIMIT_EXCEEDED`). Parked 한도 X |
| PATCH | `/goals/{id}` | 제목/마감/우선순위/tier 변경. tier 변경 시 한도 재검사 |
| POST | `/goals/{id}/decompose` | Goal Structuring Agent → `goal_nodes` 생성 (Issue #22 본 PR 은 mock stub; LLM 통합은 PR #33 + ADR-0005 §4 단계 5 후속) |
| POST | `/goals/{id}/park` | Focus → Parked |
| DELETE | `/goals/{id}` | soft delete |

응답 ID 형식: `goal_<uuid>` (§1.8). category enum 9종 (`study`/`project`/`health`/`routine`/`schedule`/`career`/`relationship`/`self_dev`/`other`).

응답 예 `POST /goals/{id}/decompose`:
```json
{
  "goalId": "goal_capstone",
  "rootNodeId": "node_root",
  "nodes": [
    { "nodeId": "node_root", "title": "캡스톤", "depth": 0 },
    { "nodeId": "node_design", "parentId": "node_root", "title": "설계 단계", "depth": 1 },
    { "nodeId": "node_impl", "parentId": "node_root", "title": "구현 단계", "depth": 1 }
  ]
}
```

---

## 7. Habits (`/habits`, `/habit-instances`) — S27

`POST /habits` 시 **이번 주 `habit_instances` 자동 생성** (cron 도입 전 임시; Issue #24 cron 후속). `frequencyPerWeek` 변경 시 `target_count` 동기화. `weekStart` 누락 시 이번 주 KST 월요일.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/habits` | 내 습관 전체 |
| POST | `/habits` | 신규 — `{ title, category, frequencyPerWeek }` |
| PATCH | `/habits/{id}` | 빈도/제목 |
| DELETE | `/habits/{id}` | soft delete |
| GET | `/habit-instances?weekStart=YYYY-MM-DD` | 이번 주 인스턴스 (`doneCount` vs `targetCount`) |
| POST | `/habit-instances/{id}/check` | 1회 달성 |

---

## 8. Planning (`/plans`) — S06, S14, S15, S16

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/plans/generate` | First Plan orchestrator(LangGraph) 실행. 입력: `outcome`(InterviewOutcome 인라인) 또는 `interviewSessionId`(+`targetDate` 선택). **빈 본문이면 최근 '정상 종료' 인터뷰(abandoned 제외)로 자동 복구** — FE 가 sessionId 를 잃어도 생성 가능(완료 인터뷰가 없으면 422). `scope`(선택, 기본 `"horizon"`): `"horizon"`=**마감까지** 전 구간(실행이 마감 전 여러 날에 분배) / `"week"`=`targetDate` 가 속한 **달력 주(월~일)** 만. `density`(선택, 기본 `"standard"`): 계획 **분량** 프리셋 — `"light"`≈주당 3세션 / `"standard"`≈5 / `"intense"`≈8. 분해(LLM) 프롬프트에 '주당 목표 세션 수' 하한으로 전달돼 생성되는 카드 수를 좌우한다(재생성 시 사용자가 조절). 어느 scope 든 이미 승인된 `scheduled_blocks` + **고정 일정(`fixed_schedules`, 수업·알바) + DB `time_policies`(온보딩 후 수정 포함)** 를 모두 busy 로 피해 배치(비파괴). Focus≤3/Maintain≤5 초과 시 422 `GOAL_TIER_LIMIT_EXCEEDED`. Draft 를 `plan_drafts`(72h)에 저장하고 실제 `planId` 반환. 응답 `isDraft=true` (#32/#62) |
| GET | `/plans/{planId}` | 저장된 Draft 미리보기 재구성(LLM 0회). 없으면 404 `PLAN_DRAFT_NOT_FOUND` (#62) |
| POST | `/plans/{planId}/approve` | HITL [수락] → SAVING. **`planId` 로 저장된 Draft 로드**(body 불필요, #62 FE 계약 변경). goals/goal_nodes/action_items/scheduled_blocks 단일 트랜잭션 영속화(+3회 재시도). **승인 = 교체**: 같은 `targetDate` 의 이전 AI 계획 산출물 중 미시작 카드(source=goal·status=planned, **user_edit 블록을 가진 카드는 보존**)와 그 블록을 soft 정리(archived/cancelled)하고, heaviest goal 의 기존 분해 트리(goal_nodes)도 보관 후 새 계획을 영속화 — 재생성→재승인 반복 시 같은 날짜 중복 누적 방지. 동시성: 시도(attempt)당 lock 재획득 + Draft 검사→영속화→승인 마킹을 **한 트랜잭션 단일 commit** 으로 묶어 동시 더블 승인의 이중 영속화 방지(lock 미획득 409 `AGENT_CONCURRENT_ACCESS`). 정책 위반 422 `PLAN_POLICY_VIOLATION` / 저장 실패 500 `PLAN_SAVE_FAILED` / 만료 410 `PLAN_DRAFT_EXPIRED`. 응답 `isDraft=false`. 부수: onboarding 완료 → `onboarding_state` 를 `ACTIVE` 로 마감(어느 온보딩 단계에서든, 멱등) (#32/#62) |
| PATCH | `/plans/{planId}/blocks/{blockId}` | 15분 snap 직접 편집 (S15) — `startAt`(필수)/`endAt` 이동 + 선택 `category`/`title` 로 목표(색·분류)·제목 수정(블록의 action_item 갱신, 같은 액션 세션 공유; 미지원 category→`other`; 정책 검사는 새 category 로). ✅ #21-B |
| POST | `/plans/{planId}/ai-edit` | 자연어 수정 (S16, P1) — diff 반환만, apply는 별도 |
| POST | `/plans/{planId}/ai-edit/apply` | diff 적용 (사용자 승인 후) |
| GET | `/plans/weekly?weekStart=YYYY-MM-DD` | 주간 그리드 (S14) — cancelled 블록(계획 교체로 취소 등)은 제외 ✅ #21-B |

> `generate`·`/plans/{planId}`·`approve`·`weekly`·블록 편집은 구현 완료. `ai-edit`/`ai-edit/apply` 만 미구현(P1, 라우트 없음).

응답 예 `POST /plans/generate` (#32, `FirstPlanResponse` — Draft Layer):
```json
{
  "isDraft": true,
  "aiSource": "llm",
  "planId": "plan_3f8c…",
  "targetDate": "2026-06-22",
  "horizon": "2026-07-12",
  "goalNodes": [
    {"nodeId": "n1", "parentId": null, "title": "캡스톤", "nodeType": "root", "orderIndex": 0, "isLeaf": true}
  ],
  "actionItems": [
    {"nodeId": "n1", "title": "저장소 세팅 30분", "estimatedMinutes": 30, "category": "study", "firstStep": "레포 clone"}
  ],
  "blocks": [
    {"start": "2026-06-22T09:00:00+09:00", "end": "2026-06-22T09:30:00+09:00", "title": "저장소 세팅 30분", "category": "study", "origin": "goal", "originId": "n1"}
  ],
  "warnings": [],
  "policyViolations": [],
  "generatedAt": "2026-06-22T08:00:00+09:00"
}
```
> `planId` 는 `plan_drafts` 에 저장된 Draft 의 실제 UUID (#62) — `GET /plans/{planId}` 로 재조회, `POST /plans/{planId}/approve` 로 승인. `aiSource` 는 LLM 분해/검토가 룰 fallback 됐으면 `"rule"`.

#21-B 구현 메모 (S14/S15 — 영속 `scheduled_blocks` 읽기/이동):
- Plan 테이블 없음 — `planId` 는 주(週) 논리 식별자(`plan_<weekStart>`). 편집 권한은 `blockId`.
- `GET /plans/weekly?weekStart=` — 그 주 월요일로 정규화(생략 시 이번 주). 7일 × `blocks[]`
  (blockId/actionId/title/category/**goalId**/startAt/endAt/blockStatus/source), KST 직렬화.
  `goalId` = 블록이 매달린 action_item 의 goal FK(`goal_<uuid>`, 미연결이면 null) — FE 가
  블록을 목표 분류(집중/유지)·색상과 연결할 수 있게 한다 (마이그레이션 없음, 기존 컬럼 노출).
- `PATCH /plans/{planId}/blocks/{blockId}` — `{ startAt, endAt? }`. **15분 snap**(가장 가까운 경계),
  `endAt` 생략 시 기존 길이 보존. 시간 충돌 422 `PLAN_BLOCK_CONFLICT`(cancelled·자기 제외),
  정책 위반 422 `POLICY_VIOLATION`(sleep/lunch/late_night_block 윈도우), 잘못된 시각 422
  `PLAN_INVALID_TIME`, 블록 없음 404 `PLAN_BLOCK_NOT_FOUND`. 적용 시 `source='user_edit'`.
- 정책 판정은 순수 함수 `orchestrator/plan_edit.py`. `no_touch`/`break_min`/freebusy·fixed_schedule
  충돌은 후속. DB 마이그레이션 없음.

---

## 9. Calendar (`/calendar`) — S04

> ⚠️ Issue #17 Alpha MVP 결정 (PM): **Google Calendar OAuth 자체를 P1 로 미룸**. `/calendar/connect` 와 `/calendar/connect` (DELETE) 는 `501 COMMON_NOT_IMPLEMENTED` 반환. FE 는 S04 에서 "수동 입력으로 시작" 경로로 안내 (`POST /fixed-schedules`). freebusy / sync-preview / approve-insert 는 Issue #18 (First Plan) 에서 실구현.

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/calendar/connect` | OAuth code → 토큰 암호화 저장 |
| DELETE | `/calendar/connect` | 연결 해제 (토큰 폐기) |
| GET | `/calendar/freebusy?from=&to=` | read-only freebusy (60s 캐시) |
| POST | `/calendar/sync-preview` | 계획 → 캘린더 이벤트 미리보기 + 충돌 체크 |
| POST | `/calendar/events/approve-insert` | 사용자 승인 일괄 삽입 (Idempotency-Key) |

가드:
- 권한 박탈/refresh 실패 → 404 `CALENDAR_NOT_CONNECTED` + 재연결 안내
- 충돌 발견 → 409 `CALENDAR_CONFLICT` (충돌 블록 목록 포함)

---

## 10. Today / Execution (`/today`) — S10~S13

| Method | Path | 설명 | 상태 |
| --- | --- | --- | --- |
| GET | `/today/agenda` | 어젠다 단일 조회 (`date` + `brief` + `cards` + `habits` + `fixedSchedules`) | ✅ #19-A |
| GET | `/today/actions/{actionItemId}` | 카드 상세 (S11) | ✅ #19-A |
| POST | `/today/actions/{actionItemId}/start` | [▶ 시작] → `execution_events` 생성 | ✅ #19-B |
| POST | `/today/focus/{executionId}/pause` | [⏸] + `interruption_events` INSERT | 🚧 #19-B-2 |
| POST | `/today/focus/{executionId}/resume` | [▶ 계속] | 🚧 #19-B-2 |
| POST | `/today/check-ins` | Quick Check-in 4칩 | ✅ #19-B (context_snapshot 캡처는 #19-B-2) |

`completion_status`: `done` / `partial_done` / `failed` / `over_done`

**#19-A 조회 (구현)**:
- `GET /today/agenda` — KST 오늘 기준. `brief`(daily_briefs, Morning Brief cron #19-C 가 채움; 없으면 null), `cards`(action_items, 오늘 target_date, priority 오름차순), `habits`(이번 주 habit_instances 진행), `fixedSchedules`(오늘 요일에 걸린 것). ID prefix `action_`/`hinst_`/`habit_`/`fixed_`
- `GET /today/actions/{id}` — `action_<uuid>`. 없으면 404 `COMMON_NOT_FOUND`
**#19-B 실행 쓰기 (구현)**:
- `POST /today/actions/{id}/start` — 미종결 scheduled_block 있으면 사용, 없으면 **즉석(ad-hoc) 블록 생성**(source=`user_edit`, §5.10)으로 NOT NULL 의존 해소. 같은 카드 in_progress 중복 시 409 `TODAY_EXECUTION_ALREADY_ACTIVE`. 응답 `{ executionId, actionId, completionStatus, actualStartAt }` (201)
- `POST /today/check-ins` — `{ executionId, completionStatus(4칩), userRating?, userFeedback? }`. execution 종결(actual_end_at·duration) + 블록 finished + **`action_item.status` 전이**(execution 레이어의 합의된 유일 지점). feedback 은 at-rest 암호화. 재체크인 409 `TODAY_ALREADY_CHECKED_IN`. 응답 `needsFailureTags=true`(failed/partial_done) → S18 → §11 태깅 → §12 Recovery 로 연결
- pause/resume(interruption_events) + context_snapshot 캡처는 #19-B-2 후속

---

## 11. Reflection (`/reflection`) — S17, S18

| Method | Path | 설명 | 상태 |
| --- | --- | --- | --- |
| GET | `/reflection/pending` | 오늘+어제+그제 미체크 카드 (3일 누적). 창 기준은 **계획 시각과 실제 착수 시각 중 나중** — 지난 블록을 뒤늦게 [▶시작] 한 카드도 착수일 기준 3일간 노출된다(#20). 창을 벗어난 카드는 매일 04:00 KST `expire_reflections` cron(`SCHEDULER_ENABLED=true` 일 때만 구동)이 같은 기준식의 여집합으로 `system_failure_reason='reflection_skipped'` + soft delete 만료하므로 목록에 나타나지 않는다 | ✅ #83 |
| POST | `/reflection/batch` | 미체크 카드 일괄 종결 (Idempotency-Key 필수). 트랜잭션 | ✅ #20 |
| GET | `/reflection/failure-tags` | 13종 마스터 (`is_active=true`) | ✅ #19-B |
| POST | `/reflection/failure-tags/{executionId}` | 0~2개 태깅 + `memoEncrypted` | ✅ #19-B |

`POST /reflection/batch` — S17 저녁 일괄 회고. 요청 `{ items: [{ executionId, completionStatus(4칩),
failureTags?(0~2), memo? }] }` (빈 배열 no-op, 상한 50건). 각 항목을 `POST /today/check-ins` 와
동일하게 종결(execution + 블록 finished + `action_item.status`)하고 failed/partial_done 항목엔
실패 사유를 함께 기록한다. **전량 사전 검증 후 단일 트랜잭션 적용** — 하나라도 무효(없음
404 `TODAY_EXECUTION_NOT_FOUND` · 이미 체크인 409 `TODAY_ALREADY_CHECKED_IN` · 중복 executionId 422
`COMMON_VALIDATION_ERROR` · non-failure 에 태그 422 `REFLECT_NOT_FAILED` · 무효 태그 422 `REFLECT_INVALID_TAG`
· 재태깅 409 `REFLECT_ALREADY_TAGGED`)면 **전체 롤백(부분 적용 없음)**. 응답
`{ processedCount, taggedCount, needsFailureTags[] }`(사유 미기록 실패 항목의 executionId). `memo` 는 서버 at-rest 암호화.

#19-B 태깅 메모: failed/partial_done 실행만 허용 (422 `REFLECT_NOT_FAILED`), 무효 코드 422
`REFLECT_INVALID_TAG`, 재태깅 409 `REFLECT_ALREADY_TAGGED` (hard delete 회피), memo 는
`encrypt_memo` at-rest 암호화. 이 태그가 §12 Recovery 룰 엔진의 입력이 된다.

13종 enum: `TIME_SHORTAGE` / `LOW_ENERGY` / `HARD_TO_START` / `PRIORITY_SHIFT`
/ `PLAN_TOO_BIG` / `FATIGUE` / `AMBIGUITY` / `CONFLICT` / `OVERRUN` / `AVOIDANCE`
/ `DISTRACTION` / `EMERGENCY` / `CONTEXT_LOSS`

---

## 12. Recovery (`/recovery`, `/replan`) — S19, S20

| Method | Path | 설명 | 상태 |
| --- | --- | --- | --- |
| POST | `/recovery/proposals/generate` | Recovery Coach (LLM ≤ 8s, 룰 fallback) → 후보 2~4개 | ✅ #20-A |
| POST | `/recovery/decisions` | 사용자 선택 저장 (Idempotency) | ✅ #20-A |
| GET | `/replan/{executionId}` | before/after diff (S20) | ✅ #20-B |
| POST | `/replan/{executionId}/approve` | 최종 적용 (Idempotency) | ✅ #20-B |

#20-A 구현 메모:
- `POST /recovery/proposals/generate` 요청 `{ executionId }` — completion_status 가
  `failed`/`partial_done` 인 실행만 허용 (422 `RECOVERY_NOT_ELIGIBLE`).
  pending 카드가 있으면 그대로 반환 (재호출 안전). 응답은 Draft Layer
  (`isDraft=true`, `aiSource=llm|rule`) + `cards[]` (attemptId/optionGroup/strategyType/
  labelKo/suggestedActionText/minRecoveryUnitMinutes/allowRestMode/triggerTag).
- 룰 선택: `recovery_strategy_catalog.primary_trigger_tags` ↔ 실패 태그 매칭,
  그룹별 최고 1장, 최소 2장 패딩 (orchestrator/recovery.py).
- `POST /recovery/decisions` 요청 `{ executionId, decision: accepted|skipped,
  acceptedAttemptId?, decisionReason? }` — accepted 시 나머지 pending 은 rejected.
  DOWNSCOPE/CARRY_OVER 수락 → 새 ActionItem(source=`recovery_downscope`/
  `recovery_carryover`, `parent_action_item_id` 혈통) 생성. RESCHEDULE/PARK 는 생성 없음.
- 에러: `RECOVERY_EXECUTION_NOT_FOUND`(404) / `RECOVERY_NOT_ELIGIBLE`(422) /
  `RECOVERY_NO_PROPOSAL`(422) / `RECOVERY_ATTEMPT_NOT_FOUND`(404) /
  `RECOVERY_ALREADY_DECIDED`(409).

#20-B 구현 메모 (replan S20):
- `GET /replan/{executionId}` — 수락한 회복의 일정 변화 프리뷰. 응답 Draft Layer
  (`isDraft=true`, `aiSource=llm|rule`) + `optionGroup` + `before`/`after`
  (각각 actionItemId/title/targetDate/startAt/endAt/estimatedMinutes, 시각은 KST)
  + `alreadyApproved`. `before`=원본 실패 카드 계획 시각, `after`=회복 카드 제안 시각
  (원본 시간대를 회복 `targetDate` 로 일(day) 단위 시프트 — 룰 기반, freebusy 무관).
- `POST /replan/{executionId}/approve` (Idempotency-Key 필수) — 회복 ActionItem 을
  `scheduled_blocks`(source=`recovery`) 로 배치. 멱등: 이미 배치돼 있으면 같은 block 반환
  (중복 INSERT 방지). 응답 `{ executionId, scheduledBlockId, actionItemId, startAt, endAt,
  isDraft=false }`. 원본 `action_item.status` 불변.
- 재배치 대상은 **새 ActionItem 을 만든 그룹(DOWNSCOPE/CARRY_OVER)** 뿐. skipped/
  RESCHEDULE/PARK 는 `RECOVERY_NO_REPLAN`(422) — RESCHEDULE/PARK 의 시간 조정은 S15
  주간 편집기에서 처리.
- 에러: `RECOVERY_EXECUTION_NOT_FOUND`(404) / `RECOVERY_NO_REPLAN`(422).

UX 4 그룹 / 내부 9 전략:
```
DOWNSCOPE  → NANO_STEP · DOWNSCOPE_DEFAULT · ENVIRONMENT_SHIFT · CONTEXT_REWARMING
RESCHEDULE → RESCHEDULE_DEFAULT · ACTIVE_RECOVERY
CARRY_OVER → CARRYOVER_DEFAULT · FREEZE_SLOT
PARK       → PARK_DEFAULT
```

원본 `action_item.status` (FAILED 등) 절대 변경 X.

---

## 13. Reviews (`/reviews`) — S21, S22

| Method | Path | 설명 | 상태 |
| --- | --- | --- | --- |
| GET | `/reviews/weekly?weekStart=YYYY-MM-DD` | 이번 주 리뷰 (일요일 03:00 precomputed) | ✅ #21-A |
| POST | `/reviews/weekly/generate` | 수동 재생성 (디버그) | ✅ #21-A |
| GET | `/reviews/habit-penalty` | 3주 미달 빈도 재설계 후보 (S22) | ✅ #21-C |
| POST | `/reviews/habit-penalty/{habitId}/accept` | 3주 미달 페널티 수락 (Idempotency) | ✅ #21-C |

핵심 필드: `adherenceRate`, `consistencyDays`, `resilienceRate`, `categorySuccessRate`,
`peakWindow`, `drainWindow`, `policyUpdateCandidates`

#21-C Habit Penalty 메모 (S22 — 비난 아닌 빈도 재설계):
- 감지: 직전 완료 주 기준 **최근 3주 연속** `done_count < target_count*0.5`. 순수 함수
  `orchestrator/habit_penalty.py`. `suggestedFrequency` = 3주 평균 달성(round, 최소 1, 현재보다 작게).
- `GET /reviews/habit-penalty` — 후보(habitId/title/current·suggestedFrequency/recentWeeks/message).
  이미 이번 사이클 결정한 habit(`last_penalty_evaluated_at` ≥ 직전 완료 주)은 제외.
- `POST /reviews/habit-penalty/{habitId}/accept` — **Idempotency-Key 필수**(§1.7 미들웨어). 조건
  미충족/중복 시 422 `HABIT_PENALTY_NOT_ELIGIBLE`, 습관 없음 404 `HABIT_NOT_FOUND`. 수락 시
  `frequency_per_week`=`target_count`=suggested, `last_penalty_decision='accepted'`. DB 마이그레이션 없음.
- reject(+4주 cooldown) 경로는 후속(현재 accept 만).

#21-A 구현 메모 (룰 기반, LLM 한 줄 평은 P2):
- `weekStart` 는 해당 주 **월요일**로 정규화(아무 날 넣어도 그 주로 스냅). 생략 시 이번 주.
  형식 오류 → 422 `REVIEW_INVALID_WEEK`.
- `GET` 은 precomputed `period_summaries`(period_type=`weekly`) 우선 반환, 없으면 **즉석 계산
  (쓰기 X)** — cron 미실행 환경(데모)에서도 빈 화면 방지. `POST generate` 만 영속화(덮어쓰기).
- 집계 소스: `execution_events`(완료/실패), `recovery_attempts`(수락=resilience 분자),
  `action_items.category`. 집계는 순수 함수 `orchestrator/weekly_review.py`.
- `resilienceRate` = 실패(`failed`/`partial_done`) 중 회복 카드 **수락** 비율(#21-A 정의).
  "회복 후 24h 내 완료" 정밀화는 #20-B(replan 완료) 데이터 확보 후.
- `restartSuccessRate`·`repeatedFailureCount`(interruption·failure_tag 조인) / `policyUpdateCandidates`(P2)
  는 #21-A 에서 `null`/`[]`.
- 일요일 03:00 KST precompute cron = `scheduler/weekly_review_precompute.py`(idempotent).
  실제 시각 트리거는 #24 운영준비에서 등록 (morning_brief 와 동일).

---

## 14. Policy Snapshot (`/policy-snapshot`)

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/policy-snapshot/current` | 현재 활성 |
| GET | `/policy-snapshot/history` | 버전 이력 |
| POST | `/policy-snapshot/preview-update` | 다음 버전 diff |
| POST | `/policy-snapshot/apply` | 사용자 승인 후 활성화 (이전은 `valid_to`) |
| POST | `/policy-snapshot/rollback/{version}` | 이전 버전 활성화 |

4 영역: `behavioralProfile` / `executionConstraints` / `interactionStyle` / `recoveryPolicy`

---

## 15. Notifications (`/notifications`) — S08

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/notifications/settings` | 내 알림 설정 |
| PATCH | `/notifications/settings` | morningTime / eveningTime / preCardEnabled |
| POST | `/notifications/subscribe` | Web Push subscription 등록 |
| DELETE | `/notifications/subscribe` | 구독 해제 |

가드:
- `morningTime` 06~10시, `eveningTime` 19~23시 외 → 422 `NOTIF_TIME_RANGE`
- 23~07시 자동 푸시 금지 (서버 측 enforce)
- 주 ≤ 3건 enforce

---

## 16. Settings / Privacy (`/settings`, `/privacy`) — S23, S28

| Method | Path | 설명 | 상태 |
| --- | --- | --- | --- |
| GET | `/settings` | 내 설정 메타 (tone, language, timezone, 알림 요약) | ✅ #23-A |
| PATCH | `/settings/tone-mode` | `gentle` / `strict` / `encouraging` | ✅ #23-A |
| GET | `/settings/profile` | 지속형 프로필 메모리 — behavioral(energyCycle·attentionSpan·timeChunkPreference·선호시각) + interaction(recoveryTone·suggestionStyle·explanationDepth·reminderFrequency). 인터뷰가 아직 안 채웠으면 각 항목 null | ✅ #A |
| PATCH | `/settings/profile` | 프로필 메모리 부분 수정 — 지정 필드만 갱신(미지정 유지), 행 없으면 생성. enum 외 값 422 | ✅ #A |
| POST | `/settings/anonymize` | 즉시 익명화 (2단계 확인 토큰 필수) | ✅ #23-B |
| GET | `/privacy/consent` | 동의 기록 | ✅ #23-B |
| POST | `/privacy/consent` | 신규 동의 (마케팅/연구 등) | ✅ #23-B |

`GET /settings` 응답:

```json
{
  "toneMode": "gentle",          // gentle|strict|encouraging|null (인터뷰 전 null)
  "language": "ko",              // MVP 잠금 (한국어 only, DevBaseline §1.4)
  "timezone": "Asia/Seoul",
  "notifications": {             // §15 알림 설정 요약. 미설정 시 null (GET 은 행 미생성)
    "morningBriefTime": "08:00",
    "eveningReflectionTime": "21:00",
    "preCardEnabled": false
  }
}
```

- `PATCH /settings/tone-mode` 요청 `{ "toneMode": "strict" }` → 갱신된 `GET /settings` 형태 반환. 그 외 값은 422 `COMMON_VALIDATION_ERROR`. onboarding 상태 전이 없음.
- `/settings/profile` — 지속형 선호(에너지·시간·톤)의 **단일 진실 소스**. 온보딩 딥 인터뷰 완료 시 자동 영속(`behavioral_profiles`·`interaction_styles`), 이후 이 endpoint 로 조회/편집(#A). 인터뷰를 다시 하지 않아도 값 변경 가능. `PATCH` 는 부분 갱신(미지정 필드 유지), 행 없으면 생성.
- 톤모드 적용: 시스템 프롬프트 prefix 1줄(`llm/prompt_compose.py`). `aiClient.run(tone_mode=...)` 배선 완료(ADR-0003 addendum 0003-llm-tool-executor.md) — **모든 LLM 호출**: inbox·recovery·morning_brief(#23-C) + interview·first_plan(#23-D, LangGraph는 config 채널).
- S28 Privacy(anonymize·consent)는 #23-B — consent 는 append-only `user_consents` 테이블(마이그레이션 동반).
- 자동 익명화: `last_active_at < now()-90d` 매일 04:00 KST → Issue #15.

#23-B 구현 메모:
- `GET /privacy/consent` — consent_type(`required`/`marketing`/`research`) 별 **최신 1행**(`{ consentType, isGranted, updatedAt }`). 미기록 시 `[]`.
- `POST /privacy/consent` `{ consentType, granted }` — **append-only** 새 행 INSERT 후 갱신 현황 반환. 잘못된 type 422 `COMMON_VALIDATION_ERROR`.
- `POST /settings/anonymize` — **2단계**: 본문 없으면 `confirmationToken` 발급(`status="confirmation_required"`, 5분 TTL, HMAC). 토큰 동봉 재요청 시 검증 후 `_encrypted` 컬럼 7종 + 이름을 `[anonymized]` 마스킹 + `is_anonymized`/`anonymized_at` set(`status="anonymized"`). 토큰 위조/만료 422 `PRIVACY_INVALID_CONFIRMATION`, 이미 익명화 409 `PRIVACY_ALREADY_ANONYMIZED`. hard delete 아님(행 보존).
- ⚠️ **새 마이그레이션** `c2d3e4f5a6b7`(user_consents) — AGENTS §8 팀 합의 동반.
- 톤 prefix 의 `aiClient.run()` 배선은 **여전히 후속**(ADR-0003 addendum) — #23-B 범위 아님.

---

## 17. Health (`/health`)

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/health` | `{ status, app, version, env, server_time }` — 인증 불필요 |

---

## 18. Inbox (`/inbox`) — S24, S25

자연어 1줄 캡처 + AI 분류(Sequential Agent) + Triage 변환. DB: `inbox_items`.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/inbox` | 내 inbox 항목. `?status=captured\|classified\|archived\|promoted` 필터 |
| POST | `/inbox` | 1줄 캡처 — `{ rawText }`. `aiClient.run("inbox/classify")` 동기 호출(8s timeout) + 룰 fallback. 응답 시 `aiCategoryGuess` 채워짐 (`status=classified`) |
| PATCH | `/inbox/{id}` | `userCategory` override (6종 enum) 또는 `status` 변경 |
| POST | `/inbox/{id}/convert-to-goal` | Goal 생성 (tier=`maintain`, 한도 enforce → 422 `GOAL_TIER_LIMIT_EXCEEDED`) + inbox `status=promoted` + `promotedGoalId` 연결 (`promotedTo="goal"`) |
| POST | `/inbox/{id}/convert-to-action` | ActionItem 생성 (`source=inbox`, `targetDate=today`) + inbox `status=promoted` (`promotedTo="action"`) |
| POST | `/inbox/{id}/archive` | soft delete (`archived_at` + `status=archived`). 이후 `?status=archived` 로 조회, `restore` 로 복원 |
| POST | `/inbox/{id}/restore` | 보관 취소 — `archived_at` 클리어 + `status`→classified/captured. 활성 항목이면 멱등. 없으면 404 `INBOX_NOT_FOUND` |

- `status`: `captured` / `classified` / `archived` / `promoted`. `GET /inbox` 는 기본 활성(archived 제외), `?status=archived` 로 보관함 조회
- `promotedTo`: `status=promoted` 일 때만 `"goal"`(promotedGoalId 로 딥링크) / `"action"`(오늘 실행 화면). 그 외 `null` — **파생 필드**(promoted + goalId 유무로 계산, DB 컬럼 아님)
- `category` enum (6종): `study` / `project` / `health` / `routine` / `schedule` / `other` (Goal/Action 9종의 subset)
- **원문(`rawText`)은 at-rest AES-256-GCM 암호화** (`raw_text_encrypted`, `safety.encrypt_inbox_text`). 응답에는 복호화된 평문
- `aiCategoryGuess` 는 LLM 호출 또는 룰 fallback 결과. `userCategory` 가 우선 (override). 둘 다 없으면 `other`
- ID prefix: `inbox_<uuid>`

---

## 19. Fixed Schedules (`/fixed-schedules`) — S05

캘린더 미연결 사용자의 수업·알바·정기 약속. DB: `fixed_schedules`.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/fixed-schedules` | 내 고정 일정 전체 |
| POST | `/fixed-schedules` | 신규 — `{ title, daysOfWeek, startTime, endTime }` |
| PATCH | `/fixed-schedules/{id}` | 부분 수정 |
| DELETE | `/fixed-schedules/{id}` | soft delete (`archived_at`) |

- `daysOfWeek`: `["mon","tue",…]` 배열. `startTime`/`endTime`: `HH:MM`
- 같은 요일 시간 겹치면 409 `FIXED_SCHEDULE_OVERLAP`. 온보딩 진행에 최소 1개 필요

---

## 20. 변경 절차

1. 변경 PR에 본 문서 수정 포함 + [`api-change-log.md`](api-change-log.md) 항목 추가
2. FE/BE 리뷰어 모두 지정
3. 기존 endpoint의 호환 깨는 변경은 `/v2/` prefix 신설 후 단계 deprecate
4. 에러 코드 신설 시 §1.4 표 갱신
5. 응답 envelope·에러·Idempotency·시간 규약 변경은 [ADR-0002](decisions/0002-api-contract-freeze.md) 수정 PR 경유
