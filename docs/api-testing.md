# API 수동 테스트 가이드

Issue #3 단계 백엔드를 로컬에서 직접 호출해 보는 방법.
계약은 [`api-contract.md`](api-contract.md), 응답 규약은 [ADR-0002](decisions/0002-api-contract-freeze.md).

## 1. 서버 실행

```bash
uv sync
uv run uvicorn reaction_backend.main:app --reload
```

| 환경 | base URL |
| --- | --- |
| local | `http://localhost:8000` |
| compose | `http://reaction-backend:8000` |
| staging / production | TBD (Issue #24 호스팅 결정 후) |

## 2. Swagger UI (권장)

`http://localhost:8000/docs` — 모든 endpoint 를 브라우저에서 직접 호출. OS 무관, 가장 쉬움.
프론트 연결 전 응답 shape 확인은 여기서.

## 3. curl 예시

> Windows PowerShell 에서는 `curl` 이 `Invoke-WebRequest` 별칭이다. `curl.exe` 를 쓰거나
> Git Bash / Swagger UI 를 사용한다.

### 헬스 체크 — 200

```bash
curl http://localhost:8000/health
# {"status":"ok","app":"reaction-backend","version":"0.1.0","env":"local",
#  "server_time":"2026-05-22T21:00:00+09:00","db":{...}}
```

### 에러 응답 형태 — 모든 에러는 ErrorResponse (ADR-0002 §2.2)

```bash
# 404 — 없는 경로
curl http://localhost:8000/nope
# {"code":"COMMON_NOT_FOUND","message":"Not Found","field":null,"server_time":"...+09:00"}

# 501 — 아직 미구현 도메인 라우터 (#3-B~#3-H 에서 mock 응답으로 채워짐)
curl.exe -X POST http://localhost:8000/auth/google
# {"code":"COMMON_NOT_IMPLEMENTED", ...}
```

### Idempotency-Key — 5개 endpoint 필수 (ADR-0002 §2.3)

대상: `POST /reflection/batch` · `/recovery/decisions` · `/replan/{id}/approve`
· `/calendar/events/approve-insert` · `/reviews/habit-penalty/{id}/accept`

```bash
# 키 누락 → 400
curl.exe -X POST http://localhost:8000/reflection/batch
# {"code":"IDEMPOTENCY_KEY_REQUIRED", ...}

# 키 동봉 → 라우터로 통과 (현재는 placeholder 501, #3-F 에서 mock 200)
curl.exe -X POST http://localhost:8000/reflection/batch -H "Idempotency-Key: demo-1"
```

같은 키 재요청은 캐시된 응답을 반환(`idempotent-replay: true` 헤더), 같은 키 + 다른 body 는 409.

## 4. 현재 상태 (Issue #3 진행 중)

- `/health` 만 실제 동작. 16개 도메인 라우터는 placeholder 501.
- 도메인 mock/stub 응답은 #3-B ~ #3-H 에서 도메인별로 채워진다.
- 데모 사용자: `demo@reaction.local` (`api/mock/demo.py`).
