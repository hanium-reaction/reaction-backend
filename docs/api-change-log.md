# API 변경 기록 (api-change-log)

[`api-contract.md`](api-contract.md) 의 버전별 변경 이력. **최신이 위.**
계약을 바꾸는 PR 은 이 파일에 항목을 추가한다 (AGENTS.md §3).

형식: `## v<버전> — <날짜> (<PR/이슈>)` + 변경 불릿. 호환 깨짐은 ⚠️ 로 표시.

---

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
