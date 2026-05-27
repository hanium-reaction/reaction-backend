# API 변경 기록 (api-change-log)

[`api-contract.md`](api-contract.md) 의 버전별 변경 이력. **최신이 위.**
계약을 바꾸는 PR 은 이 파일에 항목을 추가한다 (AGENTS.md §3).

형식: `## v<버전> — <날짜> (<PR/이슈>)` + 변경 불릿. 호환 깨짐은 ⚠️ 로 표시.

---

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
