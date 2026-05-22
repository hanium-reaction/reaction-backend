# ADR 0002 — API 응답 계약 동결 (envelope · 에러코드 · Idempotency · 시간변환)

| 항목 | 값 |
| --- | --- |
| 상태 | **Proposed** — PR #3-A 리뷰에서 확정 (PM·FE 사인오프 필요) |
| 작성일 | 2026-05-22 |
| 작성자 | claude code (#3-A, Person 1) |
| 관련 이슈 | #3 (Backend API Contract v0 + Mock/Stub Responses) |
| 관련 PR | (예정) #3-A |
| 진실 소스 | `docs/api-contract.md` · `AGENTS.md` · `Reaction_DevBaseline_v1.0` |
| 영향 범위 | 도메인 이슈 #6, #16~#25 전체 |

---

## 1. Context

re:action 은 프론트(`reaction-frontend`)와 백엔드(`reaction-backend`)가 분리된 레포다.
FE 가 백엔드 stub 에 직접 붙어 개발하므로, 응답 형식이 흔들리면 FE 작업이 연쇄로 깨진다.

Issue #3 은 도메인 이슈 **#6, #16~#25 전부의 단일 인터페이스 동결점**이다 (PM 보강 코멘트, 2026-05-22).
본 ADR 은 그 동결의 *내용*을 명시한다. 본 ADR 머지 이후 응답 envelope·에러 코드·Idempotency·시간
규약을 바꾸려면 **본 문서를 수정하는 PR** 을 거쳐야 한다 (AGENTS.md §8).

---

## 2. 결정

### 2.1 성공 응답 — envelope 없음

성공 응답(2xx)은 **도메인 객체를 직접** 반환한다. `{ ok, data }` 류 envelope 으로 감싸지 않는다.

```json
GET /goals/{id}
{ "goalId": "goal_demo_capstone", "title": "캡스톤", "goalTier": "focus" }
```

근거:
- OpenAPI 스키마가 도메인 타입과 1:1 — FE 코드 생성·타입 추론 단순
- FastAPI `response_model` 을 그대로 활용, 별도 래핑 레이어 불필요
- `AGENTS.md` §2/§3, `api-contract.md` v0.3 §1.2 가 이미 이 방식으로 잠금

### 2.2 에러 응답 — `ErrorResponse` 단일 형태

모든 에러(4xx/5xx)는 아래 한 형태로만 반환한다.

```json
{
  "code": "INTERVIEW_SLOT_LOCKED",
  "message": "이미 종료된 세션의 슬롯은 수정할 수 없어요.",
  "field": null,
  "server_time": "2026-05-22T01:23:45.678+09:00"
}
```

- `code` — **도메인 prefix + UPPER_SNAKE_CASE**. 전체 목록의 진실 소스는 `schemas/errors.py` 레지스트리.
  prefix 표는 `api-contract.md` §1.4 (`AUTH_` `USER_` `ONBOARDING_` `INTERVIEW_` `POLICY_`
  `GOAL_` `HABIT_` `PLAN_` `CALENDAR_` `EXEC_` `REFLECT_` `RECOVERY_` `REVIEW_` `NOTIF_`
  `INBOX_` `LLM_` `IDEMPOTENCY_` `COMMON_` …). 신규 도메인은 prefix 도 같이 등록.
- HTTP status 매핑: 400 / 401 / 403 / 404 / 409 / 422 / 500
- 입력 검증 실패(Pydantic `RequestValidationError`, 422)도 같은 `ErrorResponse` 로 변환 —
  `code: "COMMON_VALIDATION_ERROR"`, `field` 에 첫 위반 필드명
- 도메인 코드는 `raise ApiError(code=..., http_status=...)` 로 발생, 전역 핸들러가 `ErrorResponse` 직렬화
- 기존 `HTTPException` 도 전역 핸들러가 `ErrorResponse` 로 정규화 (status code 는 보존)

### 2.3 Idempotency

아래 5개 endpoint 는 `Idempotency-Key` 헤더 **필수**. `api/middleware/idempotency.py` 미들웨어가 처리.

| Method | Path |
| --- | --- |
| POST | `/reflection/batch` |
| POST | `/recovery/decisions` |
| POST | `/replan/{execution_id}/approve` |
| POST | `/calendar/events/approve-insert` |
| POST | `/reviews/habit-penalty/{habit_id}/accept` |

규칙:
- 같은 key 재요청 → 캐시된 응답 그대로 반환 (24h 보장)
- 같은 key + 다른 요청 body → 409 `IDEMPOTENCY_KEY_MISMATCH`
- key 누락 → 400 `IDEMPOTENCY_KEY_REQUIRED`
- **Issue #3 단계**: 저장소는 in-memory (프로세스 메모리 + 24h TTL). 영속(DB `idempotency_keys`
  테이블) 백엔드는 도메인 실구현 시 교체한다. 미들웨어의 저장소는 인터페이스로 분리해 교체 비용을 낮춘다.

### 2.4 시간 / 타임존

- 서버 내부 저장·연산: **UTC**
- API 응답의 모든 datetime 필드: **KST(+09:00) ISO 8601 with offset**
- 직렬화 시 자동 변환 — `schemas/common.py` 의 KST 직렬화 타입 적용.
  naive datetime 은 UTC 로 간주 후 KST 로 변환한다.
- 날짜만 필요한 필드(`target_date`, `week_start` 등): `YYYY-MM-DD`

### 2.5 Issue #3 본문과의 충돌 메모

Issue #3 본문 "API 공통 응답 형식" 절은 `ApiResponse<T> { ok, data?, error? }` envelope 을 명시한다.
이는 본문 작성일(2026-05-20)이 `api-contract.md` v0.3 의 envelope-less 확정(2026-05-21)보다 앞서
작성된 **stale 스니펫**이다. 본 ADR §2.1 이 이를 대체한다.
→ Issue #3 본문의 해당 절은 본 ADR 링크로 정정 필요 (PM).

---

## 3. Consequences

- (+) #6, #16~#25 가 응답 형식 결정을 매번 반복하지 않는다 — 도미노 변경 차단.
- (+) FE 가 도메인 타입에 직접 바인딩, envelope unwrap 보일러플레이트 없음.
- (−) 전역 envelope 가 없어 FE 는 성공/에러를 HTTP status 로 분기해야 한다 (REST 표준, 수용).
- (−) in-memory idempotency 는 다중 프로세스·재기동에 취약 — Issue #3 mock 한정.
  도메인 실구현 시 DB 백엔드로 교체 (후속 과제, §4).

---

## 4. 후속 작업

- `docs/api-contract.md` v0.4 — 본 ADR 반영, `/inbox`(S24)·`/fixed-schedules`(S05) 섹션 추가
- `docs/api-change-log.md` 신설
- Issue #3 본문 envelope 절 정정 (PM)
- 도메인 실구현 시 Idempotency 저장소를 DB(`idempotency_keys`)로 교체

---

## 5. 결정 기록

리뷰어가 사인오프 후 `상태`를 **Accepted** 로 갱신:

| # | 사안 | 결정 | 사인오프 |
| --- | --- | --- | --- |
| 2.1 | 성공 응답 envelope 없음 | Accepted (v0.3 추인) | ⬜ PM ⬜ FE |
| 2.2 | `ErrorResponse` 단일 에러 형태 | Accepted | ⬜ PM ⬜ FE |
| 2.3 | Idempotency-Key (5 endpoint, 24h) | Accepted | ⬜ PM |
| 2.4 | 저장 UTC / 응답 KST(+09:00) | Accepted | ⬜ PM ⬜ FE |
