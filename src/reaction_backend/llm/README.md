# `llm/` — LLM Tool Executor

**모든 LLM 호출이 거치는 단일 레이어.** 에이전트는 Gemini SDK를 직접 import 금지.

후속 이슈(#5)에서 추가될 모듈:
- `provider.py` — Gemini provider 추상화 (structured output, function calling)
- `tool_executor.py` — circuit breaker · 지수 backoff retry (최대 3회) · 8초 타임아웃 · heuristic fallback
- `cost_tracker.py` — 토큰/latency/cost를 [`../observability/`](../observability/) 의 `llm_runs` 로 기록
- `streaming.py` — Interview chat용 SSE 스트리밍 (선택)

규약:
- 모든 호출은 `(prompt_id, version, inputs) → outputs` 패턴
- 실패 시 fallback heuristic 3종은 코드 레벨에 하드코딩:
  - `plan_too_big → downscope`
  - `time_shortage → reschedule tomorrow`
  - `fatigue → carry_over + rest reminder`
- 호출 후 `safety/` 의 금지어 필터 통과 강제
