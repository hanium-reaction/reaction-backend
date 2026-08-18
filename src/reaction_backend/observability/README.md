# `observability/` — 관측성 경계와 현재 구현 지도

이 디렉터리는 로깅·LLM 실행 기록·메트릭·추적을 한 영역으로 설명하기 위한 패키지 경계다. 현재 [`__init__.py`](__init__.py)는 비어 있고, `observability/` 아래에 런타임 구현이나 직접 소비 import는 없다. 운영 중인 관측 기능은 아직 애플리케이션 진입점, LLM 안전 계층, 스케줄러 등에 나뉘어 있다.

## 현재 구성

- [`__init__.py`](__init__.py): 빈 패키지 표식이다.
- 이 README: 현재 분산된 관측 구현의 위치와 아직 없는 기능을 구분한다.

## 실제로 동작하는 관측 기능

- [`../main.py`](../main.py)의 `_configure_logging()`: Python 표준 `logging`을 INFO 레벨로 구성하고 `시간 레벨 로거명 메시지` 형식으로 stdout에 출력한다.
- [`../llm/tool_executor.py`](../llm/tool_executor.py): 폴백 사유, prompt ID/version, 사용자·trace ID를 `llm_fallback` 경고 로그에 남긴다.
- [`../safety/llm_budget.py`](../safety/llm_budget.py): 모든 `session` 포함 LLM 성공/폴백을 `llm_runs`에 INSERT한다. 모델, prompt 버전, 입력·출력 토큰, thinking을 포함한 출력 토큰, latency, 비용, 성공·폴백 여부, trace ID와 제한된 오류를 기록한다.
- [`../llm/provider.py`](../llm/provider.py): provider가 실제 반환한 모델 이름과 usage metadata를 정규화해 비용 기록의 근거를 제공한다.
- [`../safety/push_gate.py`](../safety/push_gate.py)와 scheduler 모듈: 푸시 성공·죽은 구독 정리·스윕 결과 같은 운영 이벤트를 표준 로그로 남긴다.
- LLM payload 기록을 명시적으로 켠 경우 입력·출력 요약은 [`../safety/encryption.py`](../safety/encryption.py)로 암호화되어 `llm_runs`에 저장된다.

## 핵심 계약과 불변조건

1. 관측 기능이 본 요청의 정상 동작을 깨뜨리면 안 된다. 특히 `logging`의 `module` 같은 `LogRecord` 예약 키를 `extra`에 사용하지 않고 `llm_module`처럼 충돌 없는 이름을 사용한다.
2. `llm_runs`는 INSERT only 기록이다. 기존 실행 기록을 UPDATE하지 않는다.
3. 비용 집계는 정수 센트의 반올림 손실을 피하기 위해 `cost_micro_usd`를 기준으로 한다. `cost_cents`는 DB 호환 필드다.
4. `tokens_out`에는 provider가 보고한 visible output과 thinking token이 함께 포함된다. 비용 계산에서 thinking token을 다시 더하지 않는다.
5. LLM 입력·출력 요약은 `log_payloads=True`인 경우에만 기록하고 AES-GCM 암호화 컬럼을 사용한다.
6. 오류 문자열은 `llm_runs.error` 저장 시 200자로 제한한다.
7. KST 일일 예산 집계는 [`../schemas/common.py`](../schemas/common.py)의 시간 helper를 사용한다.

## 대표 흐름

```text
애플리케이션 시작
  → main._configure_logging()이 INFO/stdout 설정

LLM 요청
  → tool_executor가 trace_id를 전달
  → provider가 모델·토큰·latency 근거를 반환
  → llm_budget.record()가 llm_runs INSERT
  → 성공은 INFO, 폴백은 사유 포함 WARNING 로그
```

## 검증

- [`test_logging_records.py`](../../../tests/test_logging_records.py): INFO 로깅 활성화와 `LogRecord` 예약 키 충돌 방지
- [`test_llm_cost_accounting.py`](../../../tests/test_llm_cost_accounting.py): 모델별 micro-USD 비용, thinking token, 미등록 모델 fallback 요율
- [`test_llm_model_pinning.py`](../../../tests/test_llm_model_pinning.py): 요청 모델과 실제 응답 모델 기록
- [`test_banned_words.py`](../../../tests/test_banned_words.py): 필터·폴백이 관측 경로를 우회하지 않는지 검증
- [`test_push_gate.py`](../../../tests/test_push_gate.py): 푸시 차단 사유와 성공 이력의 정책 경계

전체 회귀는 저장소 루트에서 `uv run pytest`로 실행한다.

## 확장 방법

- 새 관측 sink를 추가할 때는 먼저 기존 표준 로그와 `llm_runs` 소비자가 요구하는 필드를 확인하고, 민감정보를 기본적으로 수집하지 않는 스키마를 정의한다.
- request correlation을 도입하면 middleware에서 ID를 생성·검증하고, API 로그·LLM `trace_id`·background job까지 같은 값을 전달하는 회귀 테스트를 함께 추가한다.
- 메트릭을 추가하면 counter/gauge/histogram의 단위와 cardinality 상한을 문서화하고, 사용자 ID나 원문을 label로 사용하지 않는다.
- 이 패키지로 구현을 이동할 때 공개 API를 먼저 정하고 기존 호출자를 단계적으로 전환해 중복 기록을 피한다.

## 알려진 제약

- 현재 이 패키지 자체에는 `llm_runs.py`, metrics exporter, audit logger, correlation middleware가 없다.
- 로그는 JSON/jsonl 구조가 아니라 표준 텍스트 형식이다.
- Prometheus/OpenTelemetry 메트릭과 trace export는 구현되어 있지 않다.
- `X-Request-ID` 생성·전파를 보장하는 중앙 middleware가 없다. `trace_id`는 전달된 LLM 호출에서만 기록된다.
- 별도 PII masker는 없으며, 현재의 민감정보 보호는 payload 기록 opt-in과 AES-GCM 암호화에 의존한다.
- 정책 변경·익명화·토큰 발급을 한 스키마로 모으는 중앙 감사 로그는 없다.
