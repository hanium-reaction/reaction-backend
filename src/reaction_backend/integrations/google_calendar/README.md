# `integrations/google_calendar/` — Google Calendar 구현 예정 경계

이 디렉터리는 Google Calendar provider 연동을 둘 패키지 경계지만, 현재 런타임 client 구현은 없다. [`__init__.py`](__init__.py)는 비어 있고 이 패키지를 import하는 소비자도 없다. 현재 API는 수동 입력으로 시작하는 Alpha 흐름과 고정 mock 응답만 제공한다.

## 현재 구성

- [`__init__.py`](__init__.py): 빈 패키지 표식이다.
- 이 README: 실제 stub 경로와 향후 provider adapter가 지켜야 할 경계를 구분한다.

관련 구현은 현재 다른 위치에 있다.

- [`../../api/routes/calendar.py`](../../api/routes/calendar.py): connect/disconnect의 `501 COMMON_NOT_IMPLEMENTED`와 freebusy/sync-preview/approve-insert mock endpoint
- [`../../api/mock/calendar.py`](../../api/mock/calendar.py): 고정 freebusy fixture
- [`../../schemas/calendar.py`](../../schemas/calendar.py): 연결, busy interval, preview, insert 결과 API DTO
- [`../../db/models/calendar_connection.py`](../../db/models/calendar_connection.py): 향후 연결 token·상태를 담을 DB 모델
- [`../../api/middleware/idempotency.py`](../../api/middleware/idempotency.py): approve-insert 같은 변경 요청의 `Idempotency-Key` 강제 경계
- OAuth token용 AES-GCM helper는 [`../../safety/encryption.py`](../../safety/encryption.py)에 존재하지만 Calendar route와 실제 token 저장 흐름에는 아직 연결되지 않았다.

## 현재 API 동작

1. `POST /calendar/connect`: Google OAuth를 수행하지 않고 `501`과 “수동 입력으로 시작” 안내를 반환한다.
2. `DELETE /calendar/connect`: 연결 해제를 수행하지 않고 `501`을 반환한다.
3. `GET /calendar/freebusy?from=...&to=...`: 요청 범위를 실제 provider에 전달하지 않고 고정 `DEMO_FREEBUSY`를 반환한다.
4. `POST /calendar/sync-preview`: 2026-05-26의 고정 이벤트 2개와 충돌 1건을 반환한다.
5. `POST /calendar/events/approve-insert`: Google에 event를 쓰지 않고 `inserted_count=2`를 반환한다. `Idempotency-Key` 존재 여부는 공통 middleware가 검사한다.

따라서 현재 endpoint 응답은 Google Calendar와 동기화되었다는 증거로 사용하면 안 된다.

## provider 구현 시 유지할 계약

아래 항목은 현재 완료 기능이 아니라, 기존 API·보안 규약에서 이어져야 할 구현 경계다.

1. 연결 token은 평문으로 저장하지 않고 [`../../safety/encryption.py`](../../safety/encryption.py)의 `encrypt_oauth_token`/`decrypt_oauth_token`을 사용한다.
2. freebusy 조회는 사용자 시간대를 존중하되 내부 timestamp 저장·비교는 프로젝트 UTC/KST 규약에 맞춘다.
3. 실제 event insert는 사용자 preview와 명시적 승인을 거친 뒤에만 수행한다. 자동 write-back을 만들지 않는다.
4. 변경 endpoint는 `Idempotency-Key`와 scheduled block의 외부 event 식별자를 함께 사용해 중복 삽입을 막아야 한다.
5. 권한 철회·refresh 실패는 연결 상태로 명시하고 재연결 안내로 전환해야 한다. 오류를 빈 freebusy로 숨기지 않는다.
6. quota, rate limit, timeout은 유한 retry/backoff와 사용자에게 설명 가능한 오류로 처리하며 핵심 계획 트랜잭션을 부분 성공으로 남기지 않는다.

## 예상 대표 흐름

현재는 아래 흐름이 구현되어 있지 않으며, provider adapter를 추가할 때의 경계다.

```text
사용자 연결 승인
  → Google OAuth token 획득/검증
  → token 암호화 저장
  → freebusy 조회
  → 계획 충돌 preview
  → 사용자 insert 승인 + Idempotency-Key
  → events.insert
  → external event ID 저장
```

## 검증

- [`test_calendar.py`](../../../../tests/test_calendar.py): connect/disconnect 501, query validation, 고정 freebusy, 고정 preview, approve-insert의 Idempotency-Key 요구

현재 테스트는 mock API 계약을 검증할 뿐 실제 Google API, token refresh, 암호화 저장, 중복 event 방지를 검증하지 않는다. 전체 회귀는 저장소 루트에서 `uv run pytest`로 실행한다.

## 확장 방법

1. 이 패키지에 provider client와 명확한 protocol을 추가하고 route가 구체 Google SDK 대신 그 protocol에 의존하게 한다.
2. token 저장 repository를 연결해 암호화 round-trip, revoked 상태, refresh 실패를 테스트한다.
3. 고정 mock을 대체할 freebusy adapter를 추가하고 요청의 `from`/`to`, timezone, provider pagination/오류를 검증한다.
4. preview는 실제 계획 block과 busy interval의 충돌 계산으로 만들되 승인 전에는 외부 쓰기를 하지 않는다.
5. insert는 idempotency record와 외부 event ID 저장을 같은 성공 경계로 묶고, 재시도 시 이미 저장된 event를 건너뛰는 통합 테스트를 추가한다.
6. 실제 provider가 연결되면 route docstring과 이 README에서 stub 표기를 제거하고, 배포 환경의 OAuth scope·redirect URI 설정을 문서화한다.

## 알려진 제약

- Google Calendar SDK client, OAuth consent/refresh, token store, `freebusy.query`, `events.insert` 구현이 없다.
- `calendar_connections` 모델과 OAuth 암호화 helper는 존재하지만 실제 Calendar route에 배선되어 있지 않다.
- freebusy의 `from`/`to`는 현재 고정 응답에 영향을 주지 않는다.
- sync preview와 inserted count는 고정 값이며 실제 plan·calendar 상태를 반영하지 않는다.
- provider quota backoff, circuit breaker, 60초 cache, 외부 event ID 중복 가드는 구현되어 있지 않다.
