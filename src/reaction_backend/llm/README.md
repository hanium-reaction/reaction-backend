# `llm/` — LLM 호출 단일 게이트

이 패키지는 re:action 백엔드의 모든 외부 LLM 호출을 한 경로로 모은다. 오케스트레이터와 API 라우트는 Gemini SDK나 provider를 직접 호출하지 않고 `from reaction_backend.llm import aiClient`로 가져온 `aiClient.run(...)`만 사용한다.

## 현재 구성

- [`__init__.py`](__init__.py): 외부 호출자가 사용하는 `aiClient`를 다시 내보낸다.
- [`tool_executor.py`](tool_executor.py): 프롬프트 렌더링, 톤 합성, 일일 예산 검사, provider 재시도, 구조화 출력 검증, 금지어 필터, 폴백, `llm_runs` 기록을 한 흐름으로 묶는다.
- [`provider.py`](provider.py): `google.genai` 의존성을 격리하고 Gemini Structured Output을 Pydantic 모델로 검증한다. 실제 사용 모델과 입력·출력 토큰 수를 반환하며, thinking 토큰은 출력 토큰에 포함한다.
- [`prompt_compose.py`](prompt_compose.py): `gentle`, `strict`, `encouraging` 톤을 비난 없는 한 줄 시스템 프롬프트 prefix로 합성하는 순수 함수다.

## 핵심 계약과 불변조건

1. 외부 LLM 호출의 진입점은 `aiClient.run(module, schema, prompt_id, fallback, timeout=8.0, ...)` 하나다. Gemini SDK 직접 import는 `provider.py` 안으로 제한한다.
2. 응답은 호출자가 지정한 Pydantic `schema`를 통과해야 한다. provider 응답과 금지어 치환 뒤의 응답을 각각 검증한다.
3. 사용자에게 노출될 모든 성공 응답과 룰 폴백은 [`../safety/banned_words.py`](../safety/banned_words.py)의 재귀 필터를 통과한다. 성공 응답에서 필터가 `blocked=True`를 반환하면 즉시 룰 폴백으로 전환한다.
4. 프롬프트 누락·렌더 실패, 예산 초과, 시간 초과, rate limit, provider 비가용, provider/schema 오류는 호출자가 제공한 결정적 폴백으로 귀결된다. 폴백은 올바른 `schema` 인스턴스 또는 이를 반환하는 동기·비동기 callable이어야 한다.
5. 재시도 횟수는 `LLM_MAX_RETRIES` 설정을 따르며, 재시도 사이에는 최대 2초의 지수 backoff를 둔다. API key나 SDK가 없는 `ProviderUnavailable`은 재시도하지 않는다.
6. `session`이 전달된 호출은 성공과 폴백 모두 [`../safety/llm_budget.py`](../safety/llm_budget.py)를 통해 `llm_runs`에 기록한다. `session`이 없으면 DB 예산 검사와 행 기록은 하지 않고 폴백 로그만 남긴다.
7. `log_payloads=True`일 때만 프롬프트와 출력 요약을 AES-GCM으로 암호화해 저장한다. 평문 payload를 DB에 기록하지 않는다.
8. `thinking_budget`을 생략한 호출은 지연을 줄이기 위해 가능한 모델에서 thinking을 비활성화한다. 계획처럼 추론이 필요한 호출만 양수 예산과 더 긴 timeout을 함께 전달한다.
9. 톤 prefix는 표현 방식만 바꾸며, 어느 톤에서도 비난·압박·죄책감 유발 문구를 허용하지 않는다.

## 대표 실행 흐름

```text
호출자
  → prompts.registry.render(prompt_id, variables)
  → prompt_compose.compose_system_prompt(..., tone_mode)
  → safety.llm_budget.check()                 # session이 있을 때
  → provider.generate_structured()            # timeout + retry/backoff
  → safety.banned_words.enforce_structured()
  → schema 재검증
  → safety.llm_budget.record()                 # 성공/폴백 모두 기록
  → RunResult(value, fell_back, reason, ...)
```

`RunResult.reason`은 `no_prompt`, `budget`, `timeout`, `rate_limited`, `unavailable`, `validation`, `provider_error`, `banned` 중 실제 폴백 원인을 남긴다. 호출자는 정상 동작에는 `value`를 사용하고, 관측·디버깅에는 `fell_back`, 토큰 수, latency, prompt 버전, 금지어 hit를 사용할 수 있다.

## 검증

- [`test_prompt_compose.py`](../../../tests/test_prompt_compose.py): 톤 prefix와 비난 없는 카피 계약
- [`test_provider_thinking.py`](../../../tests/test_provider_thinking.py): 모델별 thinking 설정과 토큰 집계
- [`test_llm_model_pinning.py`](../../../tests/test_llm_model_pinning.py): 모듈별 모델 선택과 실제 모델 기록
- [`test_llm_cost_accounting.py`](../../../tests/test_llm_cost_accounting.py): 토큰·비용 계산과 기록
- [`test_banned_words.py`](../../../tests/test_banned_words.py): 성공/폴백의 금지어 필터 경로
- [`test_logging_records.py`](../../../tests/test_logging_records.py): LLM 로그의 예약 필드 충돌 방지
- 실제 오케스트레이터 연결은 [`test_interview_runner.py`](../../../tests/test_interview_runner.py), [`test_planning_route.py`](../../../tests/test_planning_route.py), [`test_recovery.py`](../../../tests/test_recovery.py), [`test_scheduler.py`](../../../tests/test_scheduler.py)에서 함께 검증한다.

전체 회귀는 저장소 루트에서 `uv run pytest`로 실행한다.

## 확장 방법

- 새 LLM 기능은 먼저 [`../prompts/`](../prompts/)에 버전이 붙은 프롬프트를 추가하고, 구조화 출력용 Pydantic schema와 결정적 폴백을 준비한 뒤 `aiClient.run`으로 연결한다.
- 새 `module` 값을 추가하려면 설정의 모델 매핑뿐 아니라 DB의 `LLM_MODULE_VALUES`와 관련 마이그레이션·비용 테스트를 함께 변경해야 한다.
- 새 provider를 지원하더라도 오케스트레이터에 SDK를 노출하지 말고 `provider.py` 또는 동일 레이어 내부의 어댑터 뒤에 둔다.
- 새 사용자 노출 문자열 필드를 schema에 추가해도 별도 우회 경로를 만들 필요 없이 `enforce_structured`가 트리 전체를 순회하도록 유지한다.

## 알려진 제약

- SSE/토큰 스트리밍은 구현되어 있지 않다. 현재 호출은 구조화 응답 전체가 준비된 뒤 반환한다.
- 별도 circuit breaker는 없다. 현재 복원력은 timeout, 제한된 retry/backoff, 즉시 룰 폴백으로 구성된다.
- `session=None` 호출은 DB 기반 일일 예산과 `llm_runs` 기록을 적용하지 않는다.
- 금지어로 `blocked`된 성공 응답을 LLM에 재생성 요청하지 않고 룰 폴백으로 전환한다.
- provider는 현재 Gemini 구현 하나이며, 모델별 thinking 동작 차이는 `_thinking_config`의 명시적 분기로 관리한다.
