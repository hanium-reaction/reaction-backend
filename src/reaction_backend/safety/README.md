# `safety/` — 출력 언어·암호화·예산·푸시 정책의 강제 지점

이 패키지는 권고 문서가 아니라 실제 요청 경로에서 우회할 수 없어야 하는 안전 규칙을 구현한다. 현재 범위는 사용자 노출 LLM 문구, 민감 컬럼 암호화, LLM 일일 비용 예산, Web Push 발송 상한이다.

## 현재 모듈과 책임

- [`banned_words.py`](banned_words.py): 잠금된 금지 표현을 비난 없는 권장 표현으로 치환한다. 문자열뿐 아니라 dict/list/tuple로 구성된 구조화 응답 전체를 재귀 순회한다.
- [`encryption.py`](encryption.py): `COLUMN_ENCRYPTION_KEY`에서 32-byte key를 읽어 AES-256-GCM으로 양방향 암호화한다. OAuth token, 실패 memo, LLM payload, inbox text별 associated data namespace를 분리한다.
- [`llm_budget.py`](llm_budget.py): KST 일일 token 사용량을 검사하고, 성공·폴백 호출을 append-only `llm_runs` 행으로 기록한다. 모델별 micro-USD 비용도 계산한다.
- [`push_gate.py`](push_gate.py): 모든 자동 Web Push의 구독, quiet hours, 중복, 주간 상한, 발송 결과 처리를 한 함수에서 강제한다.
- [`__init__.py`](__init__.py): 패키지 표식이다.

## 핵심 계약과 불변조건

### 사용자 노출 언어

1. 모든 LLM 성공 응답과 룰 폴백은 [`banned_words.py`](banned_words.py)의 `enforce_structured()`를 통과한다. 호출 경로는 [`../llm/tool_executor.py`](../llm/tool_executor.py) 한곳에 고정한다.
2. `BANNED_REPLACEMENTS`는 긴 표현부터 매칭해 부분 문자열 충돌을 피하고, hit 목록은 입력 순서로 중복 없이 남긴다.
3. 치환 뒤에도 금지 표현이나 `HARD_BLOCK_TERMS`가 남으면 성공 응답은 `blocked=True`가 되어 룰 폴백으로 전환된다. 폴백 자체도 재귀 치환한다.
4. 금지어 사전 변경은 카피 정책 변경이므로 코드·테스트·사람 합의를 함께 거친다.

### 민감 컬럼 암호화

1. key는 urlsafe base64를 decode했을 때 정확히 32 bytes여야 하며, 누락·형식 오류·tag 검증 실패는 `EncryptionError`로 fail fast한다.
2. 저장 형식은 `base64url(nonce 12B || ciphertext_with_tag)`다. 무작위 nonce를 매번 새로 만든다.
3. OAuth token, memo, LLM payload, inbox text는 서로 다른 associated data를 사용하므로 한 도메인의 암호문을 다른 helper로 복호화할 수 없다.
4. 익명화 sentinel `[anonymized]`는 암·복호화하지 않고 그대로 보존한다.
5. 프로세스가 cipher를 캐시하므로 key rotation 뒤에는 `get_cipher.cache_clear()`와 안전한 재암호화 절차가 필요하다.

### LLM 예산과 실행 기록

1. 일일 경계는 KST 자정이다. 사용자 호출과 `user_id=None` 시스템 호출은 별도로 합산한다.
2. 한도가 0 이하이면 예산 제한을 비활성화한다. 초과 시 provider를 부르기 전에 `BudgetExceeded`를 발생시켜 결정적 폴백으로 보낸다.
3. `llm_runs`는 INSERT only이며 commit은 호출자 트랜잭션의 책임이다.
4. 비용 집계는 `cost_micro_usd`를 사용한다. `cost_cents`는 작은 호출에서 0으로 반올림될 수 있다.
5. payload 요약은 명시적으로 제공된 경우에만 암호화 저장하고, 오류 문자열은 200자로 자른다.

### Web Push

1. 모든 자동 푸시는 `send_push()`를 통한다. cron이 sender를 직접 호출해 정책을 우회하면 안 된다.
2. 검사 순서는 구독 존재 → KST `[23:00, 07:00)` quiet hours → 사용자 advisory lock → 같은 클래스의 KST 당일 중복 → rolling 7일 실발송 3건 상한 → 실제 발송이다.
3. 사용자 lock은 이력 read와 성공 record 사이의 TOCTOU를 막는다.
4. 게이트에 막힌 시도는 예산을 소비하지 않는다. 실제 발송 성공만 이력에 기록한다.
5. push provider가 `gone`을 반환하면 죽은 subscription을 지워 반복 전송을 멈춘다.

## 대표 흐름

```text
LLM 요청
  → KST 일일 예산 check
  → provider 구조화 응답
  → 금지어 재귀 치환/차단
  → 성공 또는 룰 폴백
  → token·latency·비용·fallback을 llm_runs에 INSERT

자동 Push
  → 구독/quiet-hours 검사
  → 사용자 lock
  → 당일 class dedup + rolling 7일 3건 검사
  → sender 호출
  → 성공만 이력 기록, gone이면 구독 제거
```

## 검증

- [`test_banned_words.py`](../../../tests/test_banned_words.py): 치환 순서, 구조화 응답, 성공·폴백 경로
- [`test_privacy.py`](../../../tests/test_privacy.py): 암호화 저장과 개인정보 처리 경계
- [`test_llm_cost_accounting.py`](../../../tests/test_llm_cost_accounting.py): 예산·모델별 비용·thinking token 회계
- [`test_logging_records.py`](../../../tests/test_logging_records.py): 실행 기록 로그가 정상 호출을 깨지 않는지 검증
- [`test_push_gate.py`](../../../tests/test_push_gate.py): quiet hours 경계, 클래스 중복, 주간 상한, 동시성, 발송 결과
- [`test_web_push_sender.py`](../../../tests/test_web_push_sender.py), [`test_web_push_e2e.py`](../../../tests/test_web_push_e2e.py): provider adapter와 end-to-end 발송 경계

전체 회귀는 저장소 루트에서 `uv run pytest`로 실행한다.

## 확장 방법

- 새 사용자 노출 LLM 필드를 추가해도 별도 필터를 만들지 말고 기존 구조화 응답 schema를 통해 `enforce_structured` 경로를 유지한다.
- 새 암호화 대상에는 용도를 식별하는 고유 associated data와 typed helper를 추가하고 교차 복호화 실패 테스트를 작성한다.
- 새 LLM module은 DB 허용 값, 설정의 모델·단가, budget 기록 테스트를 동시에 갱신한다.
- 새 push class는 `NOTIFICATION_CLASSES`와 게이트 테스트를 추가하고 모든 호출자가 `send_push()`를 사용하도록 한다.
- 새 안전 정책은 여러 route에 복제하지 말고 단일 enforce 지점과 명시적 차단 사유를 만든다.

## 알려진 제약

- 별도 PII masker, 일반 사용자 입력 content validator, abuse/rate-limit 모듈은 이 패키지에 구현되어 있지 않다.
- `HARD_BLOCK_TERMS`는 현재 빈 집합이다. 현행 동작은 사전 치환이 중심이다.
- 금지어 차단 후 LLM 재생성은 하지 않고 즉시 룰 폴백으로 전환한다.
- 일일 LLM 예산 검사는 호출 전 누적량 조회이지 토큰 reservation이 아니다. 여러 동시 요청을 원자적으로 예약하는 별도 잠금은 없다.
- `session=None`인 LLM 호출은 DB 일일 예산 검사와 `llm_runs` 저장을 수행하지 않는다.
- 암호화 key rotation과 기존 행 재암호화 자동화는 이 모듈이 제공하지 않는다.
