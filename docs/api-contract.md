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

---

## 4. Interview (`/interview`) — S02 딥 인터뷰

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/interview/sessions` | 신규 세션. 이미 진행 중이면 409 `INTERVIEW_SESSION_EXISTS` |
| GET | `/interview/sessions/{id}` | 진행 상태 — `ambiguityScore`, `totalTurns`, `currentQuestion` |
| POST | `/interview/sessions/{id}/answers` | 슬롯 답 UPSERT — `{ slotKey, value, clientTurn }` |
| POST | `/interview/sessions/{id}/next-question` | 다음 질문 요청 (LLM 호출, 4초 이상 시 typing 안내) |
| POST | `/interview/sessions/{id}/finish` | 조기 종료 `[충분해요]` |
| GET | `/interview/slot-catalog` | 슬롯 카탈로그 — `slotKey·label·answerType·isRequired·category` |

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
  }
}
```

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
| POST | `/goals` | 신규. Focus 최대 3, Maintain 최대 5 |
| PATCH | `/goals/{id}` | 제목/마감/우선순위/tier 변경 |
| POST | `/goals/{id}/decompose` | Goal Structuring Agent → `goal_nodes` 생성 |
| POST | `/goals/{id}/park` | Focus → Parked |
| DELETE | `/goals/{id}` | soft delete |

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

## 7. Habits (`/habits`) — S27

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
| POST | `/plans/generate` | Goal Structuring Orchestrator 실행. 입력: 정책+goal+habit+interview+freebusy |
| GET | `/plans/{planId}` | 미리보기 (workloadLevel, conflicts, warnings) |
| POST | `/plans/{planId}/approve` | 사용자 승인 → 활성화. `INTERVIEW_REQUIRED_FIRST` 401 가능 |
| PATCH | `/plans/{planId}/blocks/{blockId}` | 15분 snap 직접 편집 (S15) |
| POST | `/plans/{planId}/ai-edit` | 자연어 수정 (S16, P1) — diff 반환만, apply는 별도 |
| POST | `/plans/{planId}/ai-edit/apply` | diff 적용 (사용자 승인 후) |
| GET | `/plans/weekly?weekStart=YYYY-MM-DD` | 주간 그리드 (S14) |

응답 예 `POST /plans/generate`:
```json
{
  "planId": "plan_2026w20",
  "horizonEnd": "2026-07-12",
  "weeks": [
    {
      "weekStart": "2026-05-18",
      "workloadLevel": "medium",
      "warnings": ["WED 6h 초과"],
      "actionItems": [...],
      "scheduledBlocks": [...]
    }
  ]
}
```

---

## 9. Calendar (`/calendar`) — S04

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

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/today/agenda` | 어젠다 단일 조회 (daily_brief + cards + habits + fixed) |
| GET | `/today/actions/{actionItemId}` | 카드 상세 (S11) |
| POST | `/today/actions/{actionItemId}/start` | [▶ 시작] → `execution_events` 생성 |
| POST | `/today/focus/{executionId}/pause` | [⏸] + `interruption_events` INSERT |
| POST | `/today/focus/{executionId}/resume` | [▶ 계속] |
| POST | `/today/check-ins` | Quick Check-in 4칩 + context_snapshot 자동 캡처 |

`completion_status`: `done` / `partial_done` / `failed` / `over_done`

---

## 11. Reflection (`/reflection`) — S17, S18

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/reflection/pending` | 오늘+어제+그제 미체크 카드 (3일 누적) |
| POST | `/reflection/batch` | 일괄 처리 (Idempotency-Key 필수). 트랜잭션 |
| GET | `/reflection/failure-tags` | 13종 마스터 (`is_active=true`) |
| POST | `/reflection/failure-tags/{executionId}` | 0~2개 태깅 + `memoEncrypted` |

13종 enum: `TIME_SHORTAGE` / `LOW_ENERGY` / `HARD_TO_START` / `PRIORITY_SHIFT`
/ `PLAN_TOO_BIG` / `FATIGUE` / `AMBIGUITY` / `CONFLICT` / `OVERRUN` / `AVOIDANCE`
/ `DISTRACTION` / `EMERGENCY` / `CONTEXT_LOSS`

---

## 12. Recovery (`/recovery`, `/replan`) — S19, S20

| Method | Path | 설명 |
| --- | --- | --- |
| POST | `/recovery/proposals/generate` | Recovery Coach (LLM ≤ 8s, 룰 fallback) → 후보 2~4개 |
| POST | `/recovery/decisions` | 사용자 선택 저장 (Idempotency) |
| GET | `/replan/{executionId}` | before/after diff (S20) |
| POST | `/replan/{executionId}/approve` | 최종 적용 (Idempotency) |

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

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/reviews/weekly?weekStart=YYYY-MM-DD` | 이번 주 리뷰 (일요일 03:00 precomputed) |
| POST | `/reviews/weekly/generate` | 수동 재생성 (디버그) |
| POST | `/reviews/habit-penalty/{habitId}/accept` | 3주 미달 페널티 수락 (Idempotency) |

핵심 필드: `adherenceRate`, `consistencyDays`, `resilienceRate`, `categorySuccessRate`,
`peakWindow`, `drainWindow`, `policyUpdateCandidates`

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

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/settings` | 내 설정 메타 (tone, language, timezone) |
| PATCH | `/settings/tone-mode` | `gentle` / `strict` / `encouraging` |
| POST | `/settings/anonymize` | 즉시 익명화 (2단계 확인 토큰 필수) |
| GET | `/privacy/consent` | 동의 기록 |
| POST | `/privacy/consent` | 신규 동의 (마케팅/연구 등) |

자동 익명화: `last_active_at < now()-90d` 매일 04:00 KST.

---

## 17. Health (`/health`)

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/health` | `{ status, app, version, env, server_time }` — 인증 불필요 |

---

## 18. Inbox (`/inbox`) — S24, S25

자연어 1줄 캡처 + AI 백그라운드 분류. DB: `inbox_items`.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/inbox` | 내 inbox 항목. `?status=captured\|classified\|archived\|promoted` 필터 |
| POST | `/inbox` | 1줄 캡처 — `{ rawText }`. AI 카테고리 추정은 백그라운드 |
| PATCH | `/inbox/{id}` | `userCategory` override 또는 `status` 변경 |
| POST | `/inbox/{id}/promote` | Goal 으로 승격 → `goals` 생성, inbox `status=promoted` |
| DELETE | `/inbox/{id}` | soft delete (`archived_at`) |

- `status`: `captured` / `classified` / `archived` / `promoted`
- 원문(`rawText`)은 at-rest 암호화 대상 (`raw_text_encrypted`)

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
