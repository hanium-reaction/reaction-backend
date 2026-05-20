# `observability/` — 로깅 / 메트릭 / 추적

후속 이슈에서 채워진다.

핵심 책임:
- `llm_runs.py` — 모든 LLM 호출의 (prompt_id, version, input_tokens, output_tokens, latency_ms, cost_cents, fallback_used, error) 저장. Cost dashboard 의 원본 데이터.
- `metrics.py` — Prometheus 형식 메트릭 (응답 시간, 에러율, LLM 성공률, recovery 수락률)
- `audit.py` — 정책 변경 / 익명화 / 토큰 발급 같은 민감 이벤트 감사 로그
- `correlation.py` — request id 전파 (X-Request-ID 헤더)

규약:
- PII는 [`../safety/pii_masker.py`](../safety/) 통과 후 기록
- 로그는 JSON 구조화 (jsonl), stdout으로
