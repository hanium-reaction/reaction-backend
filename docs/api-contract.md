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

캐시는 **(호출자, endpoint, key)** 로 스코프된다 (DB 설계의 `UNIQUE(user_id, endpoint, key)`
와 정렬). 키 값만으로 캐싱하면 body 없는 endpoint(`/replan/{id}/approve`)는 모든 호출의
body 해시가 같아 mismatch 409 로도 안 걸러지고, 다른 사용자의 응답이 그대로 재생된다.
→ **FE 는 키를 사용자·대상 스코프로 만들 것** (`replan-{executionId}` 형태. `Date.now()`
같은 전역 타임스탬프는 충돌한다).

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

## 4. Interview (`/interview`) — S02 딥 인터뷰 / S29 궁극목표 인터뷰

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/interview/sessions` | 신규 세션 + FSM 첫 질문. 본문 `{ kind? }` (`"plan"` 생략 시 기본, `"ultimate"` 궁극목표). `sessionId` 는 UUID |
| GET | `/interview/sessions/{id}` | 진행 상태 — `ambiguityScore`, `totalTurns`, `currentQuestion`. 종료 세션이면 `kind` 에 따라 `outcome` 또는 `ultimateOutcome` 동봉 |
| POST | `/interview/sessions/{id}/answers` | 슬롯 답 UPSERT — `{ slotKey, value, clientTurn }`. 종료 시 `summary`+`outcome`/`ultimateOutcome` |
| POST | `/interview/sessions/{id}/next-question` | 현재 슬롯 질문 재생성 (resume용, LLM 호출) |
| POST | `/interview/sessions/{id}/finish` | 조기 종료 `[충분해요]` → `endReason=early_user` + `outcome`/`ultimateOutcome` |
| GET | `/interview/slot-catalog?kind=plan\|ultimate` | 슬롯 카탈로그 — `slotKey·label·answerType·isRequired·category·options`. `kind` 쿼리 생략 시 `plan`(하위호환) |

응답 예: `GET /interview/sessions/{id}` (kind="plan")
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
  "outcome": null,
  "ultimateOutcome": null
}
```

- `ambiguityScore`(int) = **남은 미해결 필수 슬롯 수** (진행될수록 감소, **정상 종료 시 항상 0**). `kind` 에 따라 분모가 다르다 — `plan` 최대 18개, `ultimate` 9개. **다른 답에서 유도돼 묻지 않는 슬롯은 세지 않는다**: `goals.weekly_time`(주당 시간)은 `goals.session_length × goals.frequency` 로 계산되므로 그 둘을 답하면 질문도 안 나가고 분모에서도 빠져 `plan` 이 17개가 된다(빈도를 '몰아서 · 상관없음' 으로 답해 계산이 안 될 때만 18개). FSM 이 묻지 않는 슬롯을 세면 사용자가 채울 방법이 없어 진행바가 100%에 닿지 못한다 (v1.70).
- `currentQuestion.options` = chip/select 보기 (카탈로그 기반). `goals.heaviest`(`kind="plan"` 전용) 는 두 출처를 합쳐 동적 생성한다(ADR-0008 §8 "B"): ① 방금 답한 `goals.list` ② 사용자가 만다라 축에서 승격해 둔 목표(`GET /goals` 의 `promotedFromAxis` 카드들의 실제 `title`) — 먼저 두고, `goals.list` 응답과 겹치는 제목은 한 번만 남긴다. 승격만 해 두고 `goals.list` 에 다시 타이핑하지 않아도 이번 학기 목표로 바로 고를 수 있다. `goals.heaviest` 로 고른 제목이 `goals.list` 에 없었더라도 `core_goals`(→ `materialize_goals`)에 자동 포함된다 — 안 그러면 마감·주당시간 같은 heaviest 전용 필드가 title 매칭 실패로 유실된다. text/date/range 는 `[]`.
- 종료 턴(`endReason` 채워지고 `currentQuestion=null`)에는 `summary`(확인 카드) + `outcome`(`kind="plan"`, First Plan 시드) **또는** `ultimateOutcome`(`kind="ultimate"`, 만다라 시드)이 채워진다 — `kind` 별로 **정확히 하나만** 채워지고 나머지는 `null`. `outcome` 을 union 으로 바꾸지 않아 기존 FE 계획 인터뷰 타입은 무변경.
- 단일 활성 세션 + **재시작 승리(restart-wins)**: `POST /interview/sessions` 는 진행 중(`endReason=null`) **같은 kind** 세션이 있으면 그 세션을 `endReason=abandoned` 로 닫고 새 세션을 만든다 — **항상 201**. 다른 kind 의 진행 중 세션은 건드리지 않는다(계획 인터뷰와 궁극목표 인터뷰는 독립적으로 동시에 진행 가능). 이어하기는 저장해 둔 sessionId 로 `next-question` 재개. (v1.12 이전의 409 `INTERVIEW_SESSION_EXISTS` 는 클라이언트가 sessionId 를 잃으면 복구 불가라 폐기 — 코드 자체는 하위호환 위해 enum 에 유지.)
- 동시성 lock(ADR-0005 §7.6): 모든 mutating 진입점(`sessions`·`answers`·`next-question`·`finish`)은 `user_id × interview:{kind}` advisory lock 으로 보호(kind 별 독립 락) — 다른 디바이스가 **같은 kind** 를 점유 중이면 409 `AGENT_CONCURRENT_ACCESS` 즉시 fail.
- `kind` 값이 `"plan"`/`"ultimate"` 밖이면 요청 자체가 422 `COMMON_VALIDATION_ERROR` — 텍스트 슬롯으로 조용히 폴백하지 않는다.
- 궁극목표 인터뷰(S29, 필수 9슬롯: `ultimate.statement`·`domain`·`horizon`·`measure`·`success_image`·`identity`·`current_position`·`pillars_hint`·`constraints`, 선택 3슬롯: `values`·`assets`·`role_model`)는 계획 인터뷰와 **양방향 이월**된다: 계획 인터뷰의 `identity.*`/`recovery.*` 등은 궁극목표 인터뷰 시작 시드로, 궁극목표 인터뷰의 `ultimate.*` 전량은 계획 인터뷰 시작 시드로 회수된다 — 단 `goals.list` 같은 **다른 슬롯을 자동으로 채우지는 않는다**(그 목표는 사용자가 직접 고른다).
- 궁극목표 세션 완료는 계획 목표 영속 경로(`materialize_goals`/`supersede_proposed_goals`)를 타지 않는다 — 직전 계획 인터뷰의 잠정 목표가 지워지지 않는다.
- 구현 상태(#6, #6-B): 엔진+영속화 배선 + 단일 활성 세션(restart-wins, kind 별) + 동시성 lock(kind 별) + 궁극목표 인터뷰(kind="ultimate") 완료. **후속**: 재조립 시 transient 상태(stall_count·used_fallback) 영속. `POST /goals/ultimate`(U1, `UltimateGoalOutcome` → `Goal` 영속)는 `goal_nodes.tree_kind` 도입(§6 후속 PR)과 함께 배선된다.

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

## 6. Goals (`/goals`) — S26, S31 만다라트 상시 뷰, S32 셀 상세

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/goals` | tier별 그룹 (`focus`/`maintain`/`parked`). **잠정 목표(`status="proposed"`)도 포함**해 내려간다 — 인터뷰를 마치면 목표가 보여야 하므로(#96). FE 는 배지 등으로 구분 표시. **PR7**: 각 카드에 `isUltimate`(궁극목표 진입점 배지용)와 `promotedFromAxis`(만다라 축에서 승격된 목표면 그 축 제목, 아니면 `null` — 축 배지용)를 함께 실어, 카드마다 `GET /goals/{id}/mandala` 를 따로 부르는 N+1 을 피한다 |
| POST | `/goals` | 신규(`status="active"` 로 생성). Focus ≤ 3 / Maintain ≤ 5 (초과 시 422 `GOAL_TIER_LIMIT_EXCEEDED`). Parked 한도 X. **한도 계산에서 `proposed` 는 세지 않는다** — 아직 하기로 한 목표가 아니므로 |
| PATCH | `/goals/{id}` | 제목/마감/우선순위/tier 변경. tier 변경 시 한도 재검사 |
| GET | `/goals/{id}/nodes` | 이 목표의 **실제 분해 트리** — 계획 승인 시 영속된 `goal_nodes` 를 읽는다(보관된 옛 분해 제외, `depth`→`orderIndex` 정렬). 분해 자체는 First Plan(`planning/goal_decompose` + 마일스톤)이 수행한다. **계획을 아직 승인하지 않은 목표는 `nodes=[]`·`rootNodeId=null`** (404 아님 — 목표는 있고 분해만 없는 정상 상태). ⚠️ 이 자리에 있던 `POST /goals/{id}/decompose` 는 **제거**됐다: 목표와 무관하게 하드코딩된 데모 트리(캡스톤 → 설계/구현/발표)를 돌려주던 mock stub 이었고 FE 가 그걸 화면에 그려, 어떤 목표를 분해해도 같은 캡스톤 단계가 나왔다. **`nodeType: "milestone"`** 인 행이 섞여 나올 수 있다(ADR-0007 PR-2) — `depth=1` 로 `"subgoal"`(이번 4주 분해)과 같은 깊이를 공유하지만 `parentId=null` 이고 매 승인에도 안 바뀐다. FE 는 `nodeType` 으로 걸러 마감까지의 뼈대(마일스톤)와 이번 4주 실행 트리(subgoal/leaf)를 구분해야 한다 |
| POST | `/goals/{id}/park` | Focus → Parked |
| DELETE | `/goals/{id}` | soft delete |
| POST | `/goals/ultimate` | **궁극목표 확정**(PR5, S29→S30). 딥 인터뷰(`kind="ultimate"`) 산출물 → `Goal(status="active", goalTier="parked")`. body `{ outcome? }` — 생략하면 서버가 최근 '정상 종료' 궁극목표 인터뷰에서 복구(완료된 인터뷰가 없으면 422 `COMMON_VALIDATION_ERROR`). **사용자당 1개**(`Goal.isUltimate`) — 이미 있으면 같은 행을 갱신(409 없음, 재인터뷰로 다듬는 정상 경로). 응답은 `Goal`(위 스키마 그대로, 201). `category` 는 항상 `"other"`(궁극목표는 여러 카테고리를 가로지르므로 하나로 분류하지 않는다). `GET /goals` 의 parked 그룹에 일반 목표와 섞여 나온다(의도된 동작) — `isUltimate=true` 카드에 FE 가 만다라 진입점 배지를 붙인다(S26, PR7). **`deadline`** 은 인터뷰의 `ultimate.horizon`(3/5/7/10/10년 이상/기한 없음)에서 확정된다(ADR-0008 §2) — 오늘 + N년, "기한 없음"이면 `null`. 재인터뷰로 horizon 을 바꾸면 이 값도 같이 갱신된다. **승격된 학기 목표(U10)는 이 마감을 상속하지 않는다** — `PATCH /goals/{id}` 로 사용자가 따로 정한다 |
| GET | `/goals/{id}/mandala` | **만다라트 상시 뷰**(PR6, S31). `goal.isUltimate=true` 여야(아니면 404). 73노드(≤) + 진척도. **아직 승인된 만다라 트리가 없으면 `nodes=[]`·`rootNodeId=null`**(404 아님 — 위 `nodes` endpoint 와 같은 "정상, 그냥 비어 있음" 규약). `progress`/`coverage` 는 컬럼 캐시가 아니라 매 조회 시 파생(leaf 는 `completedAt` 직접체크 우선, 없으면 카드 성공률; 축은 leaf 8개 **고정 분모**로 나눠 "1칸 하고 100%" 착시 방지; 성공 정의는 주간 리포트 adherence 와 동일 상수 재사용) |
| PATCH | `/goals/mandala/nodes/{nodeId}` | **셀 상세 편집**(PR6, S32). body `{ title?, whyText?, completed? }` — 준 필드만 갱신, 어떤 필드든 건드리면 `source="user"` 로 전환(AI/rule 점선 렌더가 실선으로 바뀜). `completed:true`→`completedAt=now`, `false`→`null`. 제목 길이는 노드 깊이별 상한(축 10자/셀 16자) 초과 시 422 `COMMON_VALIDATION_ERROR`. 응답은 `MandalaNode` — 이 endpoint 는 롤업(`progress`/`coverage`)을 다시 계산하지 않고 `null`(필요하면 `GET /mandala` 재호출) |
| POST | `/goals/mandala/nodes/{nodeId}/promote` | **하위목표(축) 승격**(PR6, S32). body `{ goalTier }` — 그 축을 `Goal(status="proposed")` 로. **중앙(core)·셀(leaf)은 대상이 아니다**(depth≠1 이면 422). Focus≤3/Maintain≤5 초과 시 기존 422 `GOAL_TIER_LIMIT_EXCEEDED` 재사용. **멱등** — 이미 승격된 축을 다시 누르면(그 Goal 이 살아있으면) 새로 만들지 않고 그 행을 그대로 반환(201) |
| POST | `/goals/mandala/nodes/{nodeId}/habit` | **반복형 전환**(U12, ADR-0008 §1). body `{ title?, category, frequencyPerWeek, minutesPerSession, timePreference, priorityLevel }` — 새 `Habit` 을 만들어 이 칸에 링크(`habits.goalNodeId`). "코딩테스트 1일 1문제"·"쓰레기 줍기" 처럼 끝이 없는 칸을 계획(action_item)으로 안 내려보내고 습관 인프라(`habit_instances.doneCount`)로만 주간 횟수를 추적하기 위함. **칸(leaf)만 대상**(depth≠2 면 422 `COMMON_VALIDATION_ERROR`). `title` 생략 시 칸 제목 그대로. **멱등** — 이미 링크된 활성 습관이 있으면 새로 안 만들고 그대로 반환(201). 응답은 `Habit`(§7) |
| DELETE | `/goals/mandala/nodes/{nodeId}/habit` | **반복형 → 프로젝트형 되돌리기**(ADR-0008 §1). 링크된 습관을 soft delete. 칸 자체는 남는다. 링크가 없으면(이미 프로젝트형) 그냥 204(멱등, 에러 아님) |

응답 ID 형식: `goal_<uuid>` (§1.8). category enum 9종 (`study`/`project`/`health`/`routine`/`schedule`/`career`/`relationship`/`self_dev`/`other`).

`status` enum 4종 — `proposed` / `active` / `completed` / `archived`.
**`proposed`(잠정)** 는 딥 인터뷰가 추출했지만 **계획이 아직 승인되지 않은** 목표다. 인터뷰 완료 시
`proposed` 로 저장되고, `POST /plans/{planId}/approve` 가 그 목표를 `active` 로 승격한다.
승격되지 않은 잠정 목표는 두 경로로 정리된다 — ① **다음 인터뷰가 대체(보관)**, ② **14일간
미승격 시 cron 이 보관** (`expire_proposed_goals`, 매일 04:00 KST, #178) — 인터뷰 한 번 하고
돌아오지 않는 사용자에게도 탈출구가 있도록. 둘 다 soft 보관(`status='archived'`+`archived_at`)
이며 사용자 알림은 없다(ADR-0005 §7.8).
`GET /goals` 에는 계속 노출되지만 tier 한도(Focus ≤3 / Maintain ≤5)에는 포함되지 않는다.

PR7 구현 메모 (만다라 → 오늘/브리프 연결 — "만들고 나면 죽은 문서가 되는" 걸 막는 마지막 조각):
- `Goal.isUltimate`/`Goal.promotedFromAxis` 는 additive(하위호환) — `GET /goals`/`POST
  /goals/ultimate`/`POST /goals/mandala/nodes/{id}/promote` 응답에서만 실제 값을 채운다.
  `POST /goals`·`PATCH /goals/{id}`·`POST /goals/{id}/park` 응답은 `isUltimate=false`·
  `promotedFromAxis=null` 로 고정(조회 시점에 역조회하지 않음 — 다음 `GET /goals` 새로고침이
  채운다).
- **S31 진입점**(S26 목표 화면 → 만다라트 상시 뷰)은 FE 가 `isUltimate=true` 카드에 버튼을
  다는 것으로 끝난다 — 별도 endpoint 없음.
- **모닝 브리프**(`GET /today/agenda` 의 `brief`, §10)는 승격된 축 중 실제로 `active` 인
  게 있으면 그 축 이름을 headline/reasonWhyNow 어딘가에 자연스럽게 한 번 엮을 수 있다
  (LLM 재량, 프롬프트 규칙 — 없으면 언급 자체를 안 한다). **응답 스키마 변경 없음** — 기존
  자유 텍스트 필드 안의 문구만 달라진다.

응답 예 `GET /goals/{id}/nodes` (계획 승인 후). `orderIndex`/`nodeType`/`isLeaf` 는 PR6 에서
additive 로 추가됐다(만다라 렌더의 전제 — `orderIndex` 없이는 FE 가 8칸 중 몇 번째인지 모른다):
```json
{
  "goalId": "goal_abc",
  "rootNodeId": "node_11111111-...",
  "nodes": [
    { "nodeId": "node_11111111-...", "parentId": null, "title": "알고리즘 문제 풀기", "depth": 0, "orderIndex": 0, "nodeType": "core", "isLeaf": false },
    { "nodeId": "node_22222222-...", "parentId": "node_11111111-...", "title": "1주차: 입문 및 기초 문법", "depth": 1, "orderIndex": 0, "nodeType": "subgoal", "isLeaf": false },
    { "nodeId": "node_33333333-...", "parentId": "node_22222222-...", "title": "조건문 기초 문제 3개 풀기", "depth": 2, "orderIndex": 0, "nodeType": "leaf", "isLeaf": true }
  ]
}
```

응답 예 `GET /goals/{id}/mandala`(PR6, S31, `MandalaTreeResponse`). 좌표는 서버가 안 내린다 —
`(depth, parent.orderIndex, orderIndex)` 로 FE 가 계산(`SLOT=[0,1,2,3,5,6,7,8]`, 설계서 §7.3):
```json
{
  "goalId": "goal_3f8c…",
  "rootNodeId": "node_11111111-...",
  "statement": "메이저리그 8구단 드래프트 1순위",
  "nodes": [
    { "nodeId": "node_11111111-...", "parentId": null, "title": "메이저리그 8구단 드래프트 1순위",
      "depth": 0, "orderIndex": 0, "nodeType": "core", "isLeaf": false,
      "whyText": null, "source": "llm", "locked": false, "completedAt": null,
      "promotedGoalId": null, "habitId": null, "progress": 0.125, "coverage": 0.5 },
    { "nodeId": "node_22222222-...", "parentId": "node_11111111-...", "title": "구위",
      "depth": 1, "orderIndex": 0, "nodeType": "subgoal", "isLeaf": false,
      "whyText": null, "source": "user", "locked": true, "completedAt": null,
      "promotedGoalId": "goal_9c2d…", "habitId": null, "progress": 1.0, "coverage": 0.125 },
    { "nodeId": "node_33333333-...", "parentId": "node_22222222-...", "title": "주 3회 불펜피칭",
      "depth": 2, "orderIndex": 0, "nodeType": "leaf", "isLeaf": true,
      "whyText": null, "source": "llm", "locked": false, "completedAt": "2026-08-20T15:00:00+09:00",
      "promotedGoalId": null, "habitId": null, "progress": 1.0, "coverage": null },
    { "nodeId": "node_44444444-...", "parentId": "node_22222222-...", "title": "코딩테스트 1일1문제",
      "depth": 2, "orderIndex": 1, "nodeType": "leaf", "isLeaf": true,
      "whyText": null, "source": "user", "locked": false, "completedAt": null,
      "promotedGoalId": null, "habitId": "habit_7a1e…", "progress": null, "coverage": null }
  ],
  "progress": 0.125,
  "coverage": 0.5
}
```
> 아직 승인된 만다라 트리가 없으면 `{"goalId": "...", "rootNodeId": null, "statement": "...", "nodes": [], "progress": 0.0, "coverage": 0.0}`.

승인 전에는 `{"goalId": "...", "rootNodeId": null, "nodes": []}`.

`habitId` 는 additive(ADR-0008 §1) — leaf 가 `POST /goals/mandala/nodes/{id}/habit` 으로
반복형 전환됐으면 링크된 습관 id, 아니면 `null`(=프로젝트형, 기본값).

**반복형 leaf 의 롤업(ADR-0008 §1.2)**: 완료 개념이 없으므로 그 leaf 자신의
`progress`/`coverage` 는 항상 `null` (위 예시 4번째 노드). 대신 그 칸이 속한 축의 롤업에서:
- `progress`(분자) — 반복형 칸은 아예 빠진다(0으로도 안 잡는다). 그 축에 **프로젝트형
  leaf 가 하나도 없으면**(전부 반복형이거나 gaps) 축의 `progress` 는 `0.0` 이 아니라
  `null`("판단 불가" — `_leaf_progress` 가 종결 카드 없을 때 `None` 을 내는 것과 같은 규약).
- `coverage`(분모는 항상 고정 8, §7.8) — 반복형 칸은 **이번 주 그 습관을 1회 이상
  했으면**(`GET /habit-instances` 의 `doneCount > 0`) 착수로 잡힌다. 프로젝트형 leaf 의
  기존 판정(종결 카드 있음)과 합산된다.

즉 8칸이 전부 반복형인 축은 `progress: null`, `coverage`는 이번 주 실제로 건드린 반복 칸
비율로 나온다 — "0%"라는 오해를 주는 숫자가 나오지 않는다.

---

## 7. Habits (`/habits`, `/habit-instances`) — S27

`POST /habits` 시 **이번 주 `habit_instances` 자동 생성** (cron 도입 전 임시; Issue #24 cron 후속). `frequencyPerWeek` 변경 시 `target_count` 동기화. `weekStart` 누락 시 이번 주 KST 월요일.

응답 `Habit` 에 `goalNodeId`(additive, ADR-0008 §1) — 만다라 반복형 칸에서 만들어진 습관이면
그 칸(leaf) id, 일반 습관이면 `null`. 이 습관 자체를 만들거나 지우는 경로는 여전히
`§6 Goals`의 `POST`/`DELETE /goals/mandala/nodes/{nodeId}/habit` 뿐이다 — 여기 `/habits`
CRUD 로 만다라 링크를 직접 걸거나 뗄 수는 없다(만다라 칸 소유권 검증이 그쪽에 있다).

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/habits` | 내 습관 전체 |
| POST | `/habits` | 신규 — `{ title, category, frequencyPerWeek }` |
| PATCH | `/habits/{id}` | 빈도/제목 |
| DELETE | `/habits/{id}` | soft delete |
| GET | `/habit-instances?weekStart=YYYY-MM-DD` | 이번 주 인스턴스 (`doneCount` vs `targetCount`) |
| POST | `/habit-instances/{id}/check` | 1회 달성 |

---

## 8. Planning (`/plans`) — S06, S14, S15, S16, S30 만다라트 초안

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/plans/generate` | First Plan orchestrator(LangGraph) 실행. 입력: `outcome`(InterviewOutcome 인라인) 또는 `interviewSessionId`(+`targetDate` 선택). **빈 본문이면 최근 '정상 종료' 인터뷰(abandoned 제외)로 자동 복구** — FE 가 sessionId 를 잃어도 생성 가능(완료 인터뷰가 없으면 422). `scope`(선택, 기본 `"horizon"`): `"horizon"`=마감까지 전 구간, **단 한 번에 세우는 계획은 최대 4주(≈한 달)** — 마감이 그보다 멀면 4주까지만 배치하고 그 사실을 `warnings` 로 알린다(먼 미래를 자리표시자로 채우는 대신 주간 재계획이 이어받는다) / `"week"`=`targetDate` 가 속한 **달력 주(월~일)** 만. `density`(선택, 기본 `"standard"`): 계획 **분량** 프리셋 — `"light"`≈주당 3세션 / `"standard"`≈5 / `"intense"`≈8. **단 목표별 슬롯이 우선한다**: `goals.frequency`(주 N회)가 있으면 그 값이 주당 세션 수가 되고 density 는 무시되며, `goals.weekly_time`(주당 시간)만 있으면 density 는 가감 배율(0.7/1.0/1.3)로 작동한다. 둘 다 없을 때만 프리셋 그대로다. 어느 scope 든 이미 승인된 `scheduled_blocks` + **고정 일정(`fixed_schedules`, 수업·알바) + DB `time_policies`(온보딩 후 수정 포함)** 를 모두 busy 로 피해 배치(비파괴). Focus≤3/Maintain≤5 초과 시 422 `GOAL_TIER_LIMIT_EXCEEDED`. Draft 를 `plan_drafts`(72h)에 저장하고 실제 `planId` 반환. 응답 `isDraft=true`. `warnings[]` 에는 배치 실패 외에 **계획 분량 안내**가 실릴 수 있다 — 주당 시간에 못 미칠 때, **참고 자료를 링크로만 줬는데 그 링크를 열지 못했을 때**(#226 — 열었으면 본문이 분해에 실리고 이 안내는 나가지 않는다. 못 열었으면 사유를 담아 알린다: 로그인 필요·페이지 없음·형식 미지원 등), 마감까지 채우려고 회차를 덧붙였을 때, 계획이 마감 전에 끝날 때, **목표를 여러 개 말했는데 계획은 가장 무거운 것 하나만 다뤘을 때**, **`milestones` 로 확정한 중간 목표가 이번 계획 트리에 자리를 못 잡았을 때**(v1.71 — 세션 수 상한에 잘렸거나 분해가 그 branch 를 안 만든 경우. 트리에 branch 로 남아 있고 세션만 없는 건 정상이라 알리지 않는다 — 그건 `구간 커버리지` 안내가 담당). ⚠️ **한 계획은 heaviest 목표 하나만 분해·배치한다** — 나머지 목표는 세션·블록이 생기지 않는다(승인 시 목표 자체는 전부 저장된다). 한 번에 하나씩 굴리는 의도된 설계이고, 그 사실을 `warnings` 로 알린다 (#32/#62/#187) |
| GET | `/plans/{planId}` | 저장된 **First Plan** Draft 미리보기 재구성(LLM 0회). 없으면 404 `PLAN_DRAFT_NOT_FOUND` (#62). **재계획 Draft(kind=replan)를 넣어도 404** — payload 모양이 달라(goal_nodes 없음) 여기서 재구성하지 않는다. 승인 endpoint 의 같은 가드와 대칭 (#117) |
| POST | `/plans/{planId}/discard` | 계획 초안 **폐기** — "이 계획 말고 다시 인터뷰할래" 경로. 초안은 비영속(계획 블록은 승인 전 DB 에 들어가지 않는다)이라 상태 전이만 일어난다: `plan_drafts.status` 를 만료와 같은 종착 상태(`expired`)로 보낸다. **204 No Content**, 본문 없음. **멱등** — 이미 폐기·만료된 초안에 다시 호출해도 204. 이미 승인된 초안은 409 `PLAN_ALREADY_APPROVED`(승인은 되돌리는 동작이 아니다). 없는 초안·타 사용자 초안은 404 `PLAN_DRAFT_NOT_FOUND`(존재 여부를 흘리지 않으려 403 이 아닌 404). `Idempotency-Key` 불필요 |
| POST | `/plans/{planId}/approve` | HITL [수락] → SAVING. **`planId` 로 저장된 Draft 로드**(body 불필요, #62 FE 계약 변경). goals/goal_nodes/action_items/scheduled_blocks 단일 트랜잭션 영속화(+3회 재시도). **승인 = 교체**: 같은 `targetDate` 의 이전 AI 계획 산출물 중 미시작 카드(source=goal·status=planned, **user_edit 블록을 가진 카드는 보존**)와 그 블록을 soft 정리(archived/cancelled)하고, heaviest goal 의 기존 분해 트리(goal_nodes)도 보관 후 새 계획을 영속화 — 재생성→재승인 반복 시 같은 날짜 중복 누적 방지. **마일스톤(`node_type='milestone'`)은 이 교체 대상이 아니다**(ADR-0007 PR-2) — Draft 의 `milestones` 를 이때 처음 읽어 영속하고, 그 goal 에 이미 활성 마일스톤이 있으면(재승인) 손대지 않는다. `activatedGoalNodes` 는 이번 4주 트리 + 새로 만든 마일스톤 수의 합. 동시성: 시도(attempt)당 lock 재획득 + Draft 검사→영속화→승인 마킹을 **한 트랜잭션 단일 commit** 으로 묶어 동시 더블 승인의 이중 영속화 방지(lock 미획득 409 `AGENT_CONCURRENT_ACCESS`). 정책 위반 422 `PLAN_POLICY_VIOLATION` / 저장 실패 500 `PLAN_SAVE_FAILED` / 만료 410 `PLAN_DRAFT_EXPIRED`. **재계획 Draft(kind=replan)를 넣으면 404 `PLAN_DRAFT_NOT_FOUND`** — 전용 `/plans/replan/{planId}/approve` 사용(#117). 응답 `isDraft=false`. 부수: onboarding 완료 → `onboarding_state` 를 `ACTIVE` 로 마감(어느 온보딩 단계에서든, 멱등) (#32/#62) |
| PATCH | `/plans/{planId}/blocks/{blockId}` | 15분 snap 직접 편집 (S15) — `startAt`(필수)/`endAt` 이동 + 선택 `category`/`title` 로 목표(색·분류)·제목 수정(블록의 action_item 갱신, 같은 액션 세션 공유; 미지원 category→`other`; 정책 검사는 새 category 로). ✅ #21-B |
| POST | `/plans/{planId}/ai-edit` | 자연어 수정 (S16, P1) — diff 반환만, apply는 별도 |
| POST | `/plans/{planId}/ai-edit/apply` | diff 적용 (사용자 승인 후) |
| GET | `/plans/weekly?weekStart=YYYY-MM-DD` | 주간 그리드 (S14) — cancelled 블록(계획 교체로 취소 등)은 제외 ✅ #21-B |
| POST | `/plans/replan` | **주간 forward 재계획** (S21 후속). 먼저 직전 완료 주의 주간 리포트를 작성(그 회복 수락분이 백로그로 상류 반영)하고, **다음 주 월요일(`windowStart`)부터 마감까지** 남은 작업을 다시 배치. 대상 = 다음 주 이후 **미착수(`scheduled`) 블록**의 액션(actionId dedup) + **활성 블록 없는 `planned` 백로그**(수락한 회복 포함). 과거·시작/완료·`user_edit` 블록은 불변(실패 원본은 미래 블록이 없어 자동 제외). busy = 확정(시작/완료·`user_edit`) 블록 + DB `time_policies` + **고정 일정(`fixed_schedules`, #112 정합)**. 각 새 블록에 '교체할 옛 미래 블록' `replacesBlockId`(없으면 백로그라 `null`)를 실어 승인이 재조정하게 한다. `horizon` = 미래 블록·backlog `targetDate` 의 최댓값; 마감 신호가 없으면 **최소 다음 주(월~일)** 로 분산(하루 붕괴 방지), 먼 미래는 1년으로 상한. **후보 분량은 액션의 전체 live 블록 기준**: 형제 세션이 하나라도 `started`/`finished` 이거나 카드에 `user_edit` 블록이 있으면 그 액션은 **통째 보존**(후보 제외, 승인 가드와 동일 규칙), 교체되지 않고 살아남는 **미래** 블록의 분(分)은 `estimatedMinutes` 에서 차감 — 주 경계를 걸친 분할 세션이 이중 배치되지 않게. Draft 를 `plan_drafts` 에 저장, `isDraft=true`. **만료(`expiresAt`)는 기본 72h 이되 자기 `windowStart` 00:00 KST 를 넘지 않는다** — 창이 시작된 뒤 승인해 과거 블록이 생기는 것 방지(늦은 승인은 410 `PLAN_DRAFT_EXPIRED`). 동시성 lock 미획득 409 `AGENT_CONCURRENT_ACCESS` (#117) |
| POST | `/plans/replan/{planId}/approve` | 재계획 Draft 승인 → **action 단위 재조정**으로 미래 블록 교체(blanket-cancel 없음). #115 스케줄러가 긴 액션을 여러 세션 블록으로 쪼개므로, payload 의 **`oldBlocks`(액션당 옛 블록 전부)** 를 권위로 액션마다 재조정: 옛 블록 중 하나라도 `started`/`finished` → 액션 **전체 보존**(skip) / **옛 블록 중 하나라도 `source='user_edit'`(생성 후 사용자가 직접 옮김) → 액션 전체 보존**(skip, 쓰기 시점 재확인 — 생성 시점 필터만으로는 HITL 검토 창 사이 편집을 놓쳐 사용자 배치를 파괴한다) / 활성(`scheduled`) 옛 블록이 하나도 없음(그새 전부 취소·삭제) → 중복 방지 skip / 그 외 → 활성 옛 블록 **전부 취소** + 새 세션 블록 **전부 생성** / 백로그(옛 블록 없음)인데 그새 활성 블록 생김 → 생성 skip / action 이 그새 아카이브(#113) → skip. Draft 로드·검사~쓰기를 `user_agent_lock`(xact-scoped) 안 **단일 commit** 으로 원자화(동시 더블 승인 봉합, #113 패턴). 만료 410 `PLAN_DRAFT_EXPIRED`. 응답 `isDraft=false` + `{cancelledBlocks, createdBlocks, skippedBlocks}` (#117) |
| POST | `/plans/mandala/subgoals` | **만다라트 Stage A**(PR5, S30). body `{ goalId }`(`goal.isUltimate=true` 여야, 아니면 404 `GOAL_NOT_FOUND`). 궁극목표 → 하위목표(축) 8개 후보(LLM 1콜, lock 없음, DB 쓰기 0). 응답 `MandalaSubgoalsResponse`(Draft Layer) — `subgoals[8]` 은 사용자가 인터뷰에서 직접 말한 축(`pillarsHint`)이면 `locked=true`·`source="user"`, LLM 생성이면 `source="llm"`, 모자라 도메인 축 카탈로그로 채워지면 `source="rule"` |
| POST | `/plans/mandala/generate` | **Stage B**(S30). body `{ goalId, subgoals[8] }` — Stage A 를 사용자가 로컬에서 확인·편집한 8축 그대로(구조 편집은 여기까지, 이후 축 개수·순서 고정). 축마다 실행 셀 최대 8개(LLM 1콜, lock 있음, `plan_drafts`(kind="mandala") 1행·72h). 응답 `MandalaDraftResponse` — `cells[≤64]` + 못 채운 칸은 `gaps[]`(억지 패딩 없음, `goal_decompose` 와 동일 원칙) |
| GET | `/plans/mandala/{planId}` | 저장된 만다라 Draft 미리보기 재구성(LLM 0회). **First Plan/재계획 draft id 를 넣으면 404** `PLAN_DRAFT_NOT_FOUND`(kind 불일치, `GET /plans/{planId}` 의 반대 방향 같은 가드) |
| POST | `/plans/mandala/{planId}/regenerate-branch` | 링(8칸) **1개만** 재생성(LLM 1콜, lock 있음, draft UPDATE). body `{ subgoalIndex, userHint?, editedSubgoals?, editedCells? }` — 나머지 칸의 현재 편집 상태를 함께 실어 보낸다(비우면 저장된 스냅샷 사용). `source="user"`인 기존 셀(사용자가 이미 직접 편집)은 절대 재생성 대상에서 빠지지 않고 그대로 보존 |
| POST | `/plans/mandala/{planId}/approve` | 승인(LLM 0콜, 단일 트랜잭션). body `{ centerWhyText?, subgoals[8], cells[] }` — 셀 편집(HITL 최하위 층)은 여기서 처음 서버에 닿는다(승인 전엔 서버 호출 0). `goal_nodes` 최대 73행(`tree_kind="mandala"`) 영속, 같은 목표의 기존 활성 만다라 트리는 보관 후 교체(재승인 누적 방지). 응답 `{ planId, isDraft:false, goalId, rootNodeId, activated, skipped, activatedAt }`. 멱등 — 이미 승인된 draft 재호출 시 재영속화 없이 같은 결과 반환. 만료 410 `PLAN_DRAFT_EXPIRED` |
| POST | `/plans/{planId}/discard` | (재사용) 만다라 draft 폐기도 이 기존 endpoint 그대로 — 204, kind 무관 |

> `generate`·`/plans/{planId}`·`approve`·`weekly`·블록 편집·`replan`(+`replan/{id}/approve`)·만다라트(`mandala/subgoals`~`mandala/{id}/approve`)는 구현 완료. `ai-edit`/`ai-edit/apply` 만 미구현(P1, 라우트 없음).

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
  "generatedAt": "2026-06-22T08:00:00+09:00",
  "milestones": [{"title": "기초 문법", "summary": "변수·조건문·반복문"}]
}
```
> `planId` 는 `plan_drafts` 에 저장된 Draft 의 실제 UUID (#62) — `GET /plans/{planId}` 로 재조회, `POST /plans/{planId}/approve` 로 승인. `aiSource` 는 LLM 분해/검토가 룰 fallback 됐으면 `"rule"`.

`milestones` 는 additive(ADR-0007 PR-2) — 요청에 실어 보낸 확정 마일스톤을 그대로 되비출
뿐(생략하면 `[]`), `goalNodes`(이번 4주 분해)와는 별개다. **이 Draft 를 승인하면 서버가
이 목록을 다시 읽어 `node_type='milestone'` 로 영속한다** — 단 그 goal 에 아직 활성
마일스톤이 없을 때만(멱등, 재승인 시 조용히 무시). `goalNodes` 처럼 매 승인마다
갈아치워지지 않고 마감까지 살아남는다(§6 `GET /goals/{id}/nodes` 참고).

응답 예 `POST /plans/replan` (#117, `ReplanResponse` — Draft Layer). 각 블록은 기존 `actionId` 에 연결되고, `replacesBlockId` 는 이 새 블록이 교체하는 옛 미래 블록(없으면 백로그라 `null`):
```json
{
  "isDraft": true,
  "aiSource": "rule",
  "planId": "3f8c…",
  "windowStart": "2026-07-13",
  "horizon": "2026-07-17",
  "blocks": [
    {"actionId": "action_5a1b…", "title": "챕터 3 정리", "category": "study",
     "start": "2026-07-14T08:00:00+09:00", "end": "2026-07-14T08:30:00+09:00", "replacesBlockId": "block_9c2d…"},
    {"actionId": "action_7e4f…", "title": "회복: 5분만 시작", "category": "study",
     "start": "2026-07-14T18:00:00+09:00", "end": "2026-07-14T18:30:00+09:00", "replacesBlockId": null}
  ],
  "warnings": [],
  "generatedAt": "2026-07-09T12:00:00+09:00"
}
```
> 승인(`POST /plans/replan/{planId}/approve`)은 `{planId, isDraft:false, cancelledBlocks, createdBlocks, skippedBlocks, activatedAt}` 반환 — `skippedBlocks` 는 재조정으로 보존(옛 블록이 그새 시작/취소되어 교체 skip)되거나 중복 방지로 생성 skip 된 항목 수.

응답 예 `POST /plans/mandala/generate`(PR5, `MandalaDraftResponse` — Draft Layer). 좌표는 서버가 안 내린다 — `(depth, parent.orderIndex, orderIndex)` 로 FE 가 계산(`SLOT=[0,1,2,3,5,6,7,8]`, 설계서 §7.3):
```json
{
  "isDraft": true,
  "aiSource": "llm",
  "planId": "9f2c…",
  "goalId": "goal_3f8c…",
  "center": {"title": "메이저리그 8구단 드래프트 1순위", "whyText": null},
  "subgoals": [
    {"orderIndex": 0, "title": "구위", "whyText": null, "source": "user", "locked": true},
    {"orderIndex": 1, "title": "체력", "whyText": "몸이 버텨야 나머지가 의미 있다", "source": "llm", "locked": false}
  ],
  "cells": [
    {"subgoalIndex": 0, "orderIndex": 0, "title": "주 3회 불펜피칭", "source": "llm"}
  ],
  "gaps": [
    {"subgoalIndex": 1, "orderIndex": 7, "reason": "AI가 이 칸을 채우지 못했어요"}
  ],
  "generatedAt": "2026-08-20T15:00:00+09:00"
}
```
> 승인(`POST /plans/mandala/{planId}/approve`)은 `{planId, isDraft:false, goalId, rootNodeId, activated, skipped, activatedAt}` 반환 — `rootNodeId` 는 `node_<uuid>`(§1.8), `activated`=1(중앙)+8(축)+영속된 셀 수, `skipped`=최대 64칸 중 저장 안 된 칸 수(gaps 로 남은 만큼).

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
| POST | `/today/actions/{actionItemId}/cancel` | 카드 취소 = soft delete (`archived_at`, **status 불변**) | ✅ #214 |
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

**카드 취소 (#214)**:
- `POST /today/actions/{id}/cancel` → **204**. `archived_at` 만 세팅하고 **`status` 는 바꾸지 않는다**(AGENTS §2 — 원본 status 는 Resilience 지표의 전제). 조회가 전부 `archived_at IS NULL` 로 걸러 오늘 어젠다·백로그에서 빠진다
- **취소 가능 조건 3개 전부**: `status='planned'` + 실행 이력 없음 + `source ∈ {inbox, manual}`. `recovery_*` 는 `resulting_action_item_id` 로 회복 지표와 얽혀 있고, `goal`/`habit` 파생은 계획 정합성이 걸려 있어 제외
- 조건에 안 맞으면 422 `COMMON_VALIDATION_ERROR`(`field="actionId"`), **사유별로 다른 message** — '이미 시작한 일' 과 '계획에 묶임' 을 FE 가 구분해 안내할 수 있게
- **이미 취소된 카드에 다시 호출해도 204**(멱등). 없는 카드는 404 `COMMON_NOT_FOUND`
- 되돌리기(restore)는 **없다** — 자료의 걸음에서 다시 담으면 복구된다. FE 는 취소 직후 5초 스낵바를 띄우고 **그 뒤에** 이 API 를 호출한다(되돌리면 요청 자체를 안 보냄)
- `AgendaCard.cancellable` (파생 필드, DB 컬럼 아님) — 위 3조건의 판정 결과. **판정 규칙은 서버에만 있다**(`domain/action_cancel.py`): FE 는 `status`·`source` 만 받아 '실행 이력 없음' 을 계산할 수 없고, 규칙을 복제하면 바뀔 때 조용히 어긋난다
- 지표 영향 없음 — 취소 가능한 카드는 정의상 실행 이력이 없어 주간 KPI(`execution_events` join)에 애초에 들어간 적이 없다

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

**일별 '하루 에너지'는 이 계약에 없다 — 설계에 없기 때문이다** (#141). S17 회고는 **실행 단위
종결**만 다룬다. 이 문단이 없어서 "저장할 자리가 있는데 BE 가 안 만들었다"는 오해가 반복됐다:

- `context_snapshots.estimated_energy_level` 은 **하루 총평이 아니라 실행 1건의 그 순간 추정치**다
  (`focus_level`/`overwhelm_level`/`noise_level` 과 같은 "state 1~5 척도" 4형제, DB 설계서 §5.18).
  쓰는 주체·시점도 다르다 — architecture.md §5 표: `Execution Logger | check-in 입력 |
  execution_event + context_snapshot`. **캡처 자체가 #19-B-2 유예 중**(위 §10 `/today/check-ins` 행).
  `execution_id` 가 NOT NULL 이라 **미체크 실행이 0건인 날엔 저장할 행 자체가 없다**.
- 지속형 에너지 **선호**의 단일 진실 소스는 `/settings/profile`(`behavioral_profiles.energy_cycle`,
  §17). 인터뷰가 채우며 일별 기록과는 다른 개념이다.

즉 일별 에너지를 도입하려면 **새 저장소 + 마이그레이션**(AGENTS.md §8 사람 합의 대상)이고, 그 전에
**읽을 사람(소비처)부터 정해야 한다** — `context_snapshots` 가 설계·마이그레이션·모델을 다 갖추고도
INSERT/SELECT 0곳인 채 남아 있는 게 "저장부터 하면 언젠가 읽는다"가 거짓이라는 레포 내부 물증이다.

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
| POST | `/recovery/proposals/generate` | Recovery Coach (LLM thinking 0 + ≤ 12s — ADR-0003 addendum(#128), 룰 fallback) → 후보 2~4개 | ✅ #20-A |
| POST | `/recovery/decisions` | 사용자 선택 저장 — 수락/**수정**/스킵 (Idempotency) | ✅ #20-A |
| GET | `/replan/{executionId}` | before/after diff (S20) | ✅ #20-B |
| POST | `/replan/{executionId}/approve` | 최종 적용 (Idempotency) | ✅ #20-B |

#20-A 구현 메모:
- `POST /recovery/proposals/generate` 요청 `{ executionId }` — completion_status 가
  `failed`/`partial_done` 인 실행만 허용 (422 `RECOVERY_NOT_ELIGIBLE`).
  pending 카드가 있으면 그대로 반환 (재호출 안전). 응답은 Draft Layer
  (`isDraft=true`, `aiSource=llm|rule`) + `cards[]` (attemptId/optionGroup/strategyType/
  labelKo/suggestedActionText/minRecoveryUnitMinutes/allowRestMode/triggerTag).
  **이미 결정된 실행(pending 0건 + 결정 이력 있음)은 `RECOVERY_ALREADY_DECIDED`(409)** —
  회복 카드 세트는 실행 1건당 1세트다. 재생성을 허용하면 `/recovery/decisions` 의 409 가
  무력화돼 같은 실패에 회복 ActionItem 이 여러 개 생기고, replan 은 `created_at` 오름차순의
  **첫** 채택 카드에 고정돼 사용자가 다시 고른 최신 회복이 영영 배치되지 않는다.
  → FE 는 회복 화면 재진입 시 409 를 "이미 결정함"으로 처리한다(에러 토스트 X).
- 룰 선택: `recovery_strategy_catalog.primary_trigger_tags` ↔ 실패 태그 매칭,
  그룹별 최고 1장, 최소 2장 패딩 (orchestrator/recovery.py).
- `POST /recovery/decisions` 요청 `{ executionId, decision: accepted|edited|skipped,
  acceptedAttemptId?, editedActionText?, decisionReason? }` — accepted 시 나머지 pending 은
  rejected. DOWNSCOPE/CARRY_OVER 수락 → 새 ActionItem(source=`recovery_downscope`/
  `recovery_carryover`, `parent_action_item_id` 혈통) 생성. RESCHEDULE/PARK 는 생성 없음.
- **`decision="edited"`(잠금 결정 [수락/수정/거절] 의 '수정')** — `acceptedAttemptId` +
  `editedActionText`(trim 후 1~300자) 필수. 부수효과는 accepted 와 **동일**(형제 rejected,
  새 ActionItem 생성, replan 대상)이고 **새 카드 title 만 사용자 문구**가 된다.
  AI 원문 `suggestedActionText` 는 **보존**한다(덮어쓰지 않음) — "AI 제안을 얼마나 고쳐
  썼나"가 Draft Layer 의 효과 지표다. 사용자 문구는 금지어 필터를 거치지 않는다(톤 잠금은
  AI 출력 대상). 새 카드를 안 만드는 RESCHEDULE/PARK 은 문구를 담을 곳이 없어 422 —
  이 그룹의 조정은 문구가 아니라 시간이고 S15 주간 편집기(`PATCH /plans/{planId}/blocks/{blockId}`)
  소관이다. 응답 스키마는 **불변**(채택 카드는 `acceptedAttemptId` 로 반환).
- 회복 지표(`resilience_rate`·`average_recovery_minutes`)는 `accepted` 와 `edited` 를
  **함께** 회복으로 센다.
- 에러: `RECOVERY_EXECUTION_NOT_FOUND`(404) / `RECOVERY_NOT_ELIGIBLE`(422) /
  `RECOVERY_NO_PROPOSAL`(422) / `RECOVERY_ATTEMPT_NOT_FOUND`(404) /
  `RECOVERY_ALREADY_DECIDED`(409) / `RECOVERY_EDIT_NOT_SUPPORTED`(422).

#20-B 구현 메모 (replan S20):
- `GET /replan/{executionId}` — 수락한 회복의 일정 변화 프리뷰. 응답 Draft Layer
  (`isDraft=true`, `aiSource=llm|rule`) + `optionGroup` + `before`/`after`
  (각각 actionItemId/title/targetDate/startAt/endAt/estimatedMinutes, 시각은 KST)
  + `alreadyApproved`. `before`=원본 실패 카드 계획 시각, `after`=회복 카드 제안 시각
  (원본 시간대를 회복 `targetDate` 로 일(day) 단위 시프트 — 룰 기반, freebusy 무관).
  **날짜는 시프트가 정하고, 시각은 그 날 안에서만 보정한다** — 시프트 결과가 이미 지난
  시각이면 `조회/승인 시각 + 10분`을 15분 격자로 올린 시각까지 앞당긴다. 보정은 (a) 같은
  KST 날짜 안이고 (b) 보정된 블록이 그 날 **23:00**(알림 quiet hours 시작과 같은 경계) 전에
  끝날 때만 한다. 둘 중 하나라도 어긋나면 보정하지 않는다 — 회복 `targetDate` 는 어떤
  경우에도 바뀌지 않아 카드 날짜와 블록 날짜는 항상 같은 날이다.
  왜: 회복 결정은 21시 일괄 회고(잠금 결정)에서만 일어나고 DOWNSCOPE 는 day_delta 가 0 이라,
  보정이 없으면 결과가 항상 **이미 지나간 원본 슬롯**이 된다. 과거 블록은 `pre_card` 알림
  창(`[now+2m, now+7m)`, 5분 폴)을 영영 만나지 못한다. 왜 밤엔 안 미는가: 블록 생성 경로는
  시간 정책 검사를 하지 않는데 S15 주간 편집기는 같은 시각을 `POLICY_VIOLATION`(422)으로
  거부한다 — 서버가 사용자보다 느슨한 블록을 만들지 않기 위한 하한선.
  `freebusy`·`time_policies` 는 **여전히 보지 않는다**(명시적 비목표) — 방금 승인된 5~30분
  행동이라 슬롯 탐색을 하지 않는다. 정책 인지 배치는 후속.
  `alreadyApproved=true` 면 `after.startAt`/`endAt` 는 **실제 배치된 블록** 시각이다.
  미승인 프리뷰는 "지금 승인하면 여기"라 조회 시각에 따라 달라진다.
- `POST /replan/{executionId}/approve` (Idempotency-Key 필수) — 회복 ActionItem 을
  `scheduled_blocks`(source=`recovery`) 로 배치. 멱등: 이미 배치돼 있으면 같은 block 반환
  (중복 INSERT 방지). 응답 `{ executionId, scheduledBlockId, actionItemId, startAt, endAt,
  isDraft=false }`. 원본 `action_item.status` 불변.
  멱등·`alreadyApproved` 판정은 **블록 소스와 무관**하게 그 회복 카드의 미취소 블록 유무로
  한다 — S15 이동이 `source`를 `user_edit` 로 덮거나 주간 재계획이 `ai_plan` 으로 만들어도
  '이미 배치됨'이다. 소스로 거르면 CTA 가 되살아나 블록이 중복 생성된다.
- 재배치 대상은 **새 ActionItem 을 만든 그룹(DOWNSCOPE/CARRY_OVER)** 뿐. skipped/
  RESCHEDULE/PARK 는 `RECOVERY_NO_REPLAN`(422) — RESCHEDULE/PARK 의 시간 조정은 S15
  주간 편집기에서 처리.
- 에러: `RECOVERY_EXECUTION_NOT_FOUND`(404) / `RECOVERY_NO_REPLAN`(422).

UX 4 그룹 / 내부 13 전략 (v1.60, #257 — 태그 구멍 3개 + PARK 도달 경로 gap-fill):
```
DOWNSCOPE  → NANO_STEP · DOWNSCOPE_DEFAULT · ENVIRONMENT_SHIFT · CONTEXT_REWARMING
           · SELF_FORGIVENESS_NANO(신설)
RESCHEDULE → RESCHEDULE_DEFAULT · ACTIVE_RECOVERY · TIMEBOX_REBUDGET(신설) · BUFFER_INSERT(신설)
CARRY_OVER → CARRYOVER_DEFAULT · FREEZE_SLOT
PARK       → PARK_DEFAULT · GOAL_RECHECK(신설)
```
GOAL_RECHECK(정적 태그: AVOIDANCE, PRIORITY_SHIFT) 가 PARK 그룹의 실제 도달 경로다.
PARK_DEFAULT 는 여전히 정적 태그가 없다(동적 조건 overwhelm≥4 미구현).

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
| GET | `/notifications/vapid-public-key` | FE `applicationServerKey` 용 공개키 |
| POST | `/notifications/subscribe` | Web Push subscription 등록 (201, 갱신된 설정 반환) |
| DELETE | `/notifications/subscribe` | 구독 해제 (204, 멱등 — 구독 없어도 204) |

`GET /notifications/vapid-public-key` → `{ "publicKey": string | null }`:

- `publicKey` 는 서버 private key 의 **짝**. FE 는 구독 직전 이걸 **런타임에 받아** 쓴다 —
  하드코딩·빌드타임 주입은 키 rotate 시 조용히 깨진다(구독은 옛 키에 묶인 채 발송이 push
  서비스 403 으로 전부 실패, 구독 자체는 성공하므로 알아채기 어렵다).
- `publicKey: null` = 서버에 VAPID 미설정. FE 는 **구독을 만들지 말고** '알림 미지원' 표시.
- 인증 필수(구독 흐름 자체가 로그인 후). 값은 사용자 무관한 서버 상수.

`POST /notifications/subscribe` 요청 = 브라우저 `PushSubscription.toJSON()`:

```json
{
  "endpoint": "https://fcm.googleapis.com/…",
  "keys": { "p256dh": "…", "auth": "…" }
}
```

- `keys.p256dh` / `keys.auth` 누락·빈 값 → 422 `COMMON_VALIDATION_ERROR` (발송 암호화에 필수)
- 재구독은 덮어쓰기 (1 device 1 subscription — Issue #16)
- 응답은 `GET /notifications/settings` 와 같은 형태. `pushSubscribed` 는 저장된 구독 유무에서 파생

가드 (서버 측 enforce — 발송 게이트 `safety/push_gate.py` 단일 지점, ADR-0006):
- `morningTime` 06~10시, `eveningTime` 19~23시 외 → 422 `NOTIF_TIME_RANGE`
- 23~07시 자동 푸시 금지 — 구간은 `[23:00, 07:00)`. `eveningTime` 이 22:55 를 넘으면
  **22:55 로 클램프해 발송**한다 (quiet hours 전 마지막 5분 폴 — 미발송 사각지대 방지,
  ADR-0006 §7). FE 는 22:55 상한 노출 권장
- 주 ≤ 3건 — **사용자별 · 3 클래스(morning_brief/pre_card/evening_reflection) 합산 ·
  rolling 7일 · 실발송만 카운트**. cron 은 매일 시도하지만 실제 수신은 주 3건이 상한이다
  (알림 피로 최소화 — 베이스라인 §1.4 잠금의 문면 그대로, 해석 근거 ADR-0006 §2)
- 같은 클래스 하루(KST) 1건 — "24h 중복 금지"의 달력일 구현 (래칫 방지, ADR-0006 §3)
- 저녁 회고 알림은 **회고할 카드가 있을 때만** (경계는 `GET /reflection/pending` 과 동일)
- pre_card 는 opt-in(`preCardEnabled`) + 시작 2~7분 전 (2분 리드 + 5분 폴)

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
| GET | `/inbox/resources/{slug}` | 시스템 항목이 가리키는 정적 자료 본문 — `{ slug, title, markdown, steps }`. **인증만 필요하고 소유권 검사는 하지 않는다**(레포에 커밋된 공용 콘텐츠라 소유권 개념이 없다). 없으면 404 `COMMON_NOT_FOUND` |
| POST | `/inbox/{id}/adopt-step` | 자료가 제안한 한 걸음을 오늘 할 일로 채택 — `{ stepIndex }` → `{ actionId, title, targetDate, resourceSlug }`. `ActionItem(source=inbox)` 생성 + `inbox_item_id` 로 자료에 연결. 카드의 `category` 는 **자료의 카테고리**(9종 원본) — `userCategory` 재분류가 있으면 그게 우선. **자료 항목은 promoted 로 바뀌지 않는다**(다른 걸음을 또 채택하거나 다시 읽을 수 있다). **도메인 멱등(#213)**: 같은 걸음(`stepIndex` 동일)을 오늘 다시 채택하면 새 카드를 만들지 않고 기존 활성 카드의 `actionId` 를 200 으로 반환 — 날짜가 바뀌거나 카드가 보관된 뒤에는 다시 새 카드가 생긴다. 없거나 보관된 항목이면 404 `INBOX_NOT_FOUND`, 자료 파일이 사라졌으면 404 `COMMON_NOT_FOUND`, system 항목이 아니면 422 `COMMON_VALIDATION_ERROR`(`field=inboxId`), 없는 인덱스면 422(`field=stepIndex`) |

- `status`: `captured` / `classified` / `archived` / `promoted`. `GET /inbox` 는 기본 활성(archived 제외), `?status=archived` 로 보관함 조회
- `promotedTo`: `status=promoted` 일 때만 `"goal"`(promotedGoalId 로 딥링크) / `"action"`(오늘 실행 화면). 그 외 `null` — **파생 필드**(promoted + goalId 유무로 계산, DB 컬럼 아님)
- `category` enum (6종): `study` / `project` / `health` / `routine` / `schedule` / `other` (Goal/Action 9종의 subset)
- **원문(`rawText`)은 at-rest AES-256-GCM 암호화** (`raw_text_encrypted`, `safety.encrypt_inbox_text`). 응답에는 복호화된 평문
- `aiCategoryGuess` 는 LLM 호출 또는 룰 fallback 결과. `userCategory` 가 우선 (override). 둘 다 없으면 `other`
- ID prefix: `inbox_<uuid>`
- `source` (#171): `user`(사용자 캡처) / `system`(목표 카테고리에 맞춰 자동으로 넣어 준 추천 자료). `promotedTo` 와 달리 **저장 컬럼 그대로**이며 파생 필드가 아니다
- `resourceSlug` (#171): system 항목이 가리키는 자료 slug. user 항목은 `null`. **ID prefix 를 붙이지 않는다** — 그대로 `GET /inbox/resources/{slug}` 경로에 들어간다
- **system 항목은 승격할 수 없다** — `convert-to-goal` / `convert-to-action` 둘 다 422 `COMMON_VALIDATION_ERROR` (`field="inboxId"`). 자료를 목표로 올리는 건 의미가 없고, 자료→목표→새 자료 재귀가 생긴다
- `steps` (#171 후속): 자료가 제안하는 한 걸음 1~5개. 채택하면 오늘 할 일이 되고 그 뒤는 **기존 실행·회고·회복 루프가 그대로** 처리한다 — 회복은 여전히 21시 일괄 회고에서만 시작된다
- 자료 삽입은 목표가 **active 로 생긴 순간**에만, 카테고리당 1건, 멱등(**보관한 것도 '이미 있음'**). 삽입 실패는 목표 생성을 막지 않는다(best-effort)

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
