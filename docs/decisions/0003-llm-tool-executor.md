# ADR-0003 — LLM Tool Executor 단일 게이트 + 동결 호출 시그니처

- 상태: Accepted (소급 문서화, 2026-06-23) · Addendum 1 (#23-C tone_mode)
- 관련: AGENTS.md §2(LLM SDK 직접 import 금지), ADR-0005(Agentic Architecture), Issue #5/#23

> 이 문서는 코드 전반이 "ADR-0003"으로 참조해 온 계약을 소급 기록한다. 계약 본체는
> `src/reaction_backend/llm/tool_executor.py` 에 살아 있었고, 본 ADR 은 그것을 명문화한다.

## 맥락

모든 LLM 호출은 Gemini SDK 를 직접 import 하지 않고 **단일 게이트** `aiClient.run()`
(`llm/tool_executor.LLMToolExecutor`) 만 통과한다 (AGENTS.md §2). 이 게이트가 프롬프트 렌더,
예산 가드(`llm_budget`), provider 호출 + 재시도/backoff, 금지어 후처리(`safety.banned_words`),
`llm_runs` 기록, 8초 timeout → 룰 fallback 을 일괄 책임진다.

## 결정

### §1. 동결 호출 시그니처

```python
async def run[T: BaseModel](
    module, schema, prompt_id, fallback, timeout=8.0, *,
    variables=None, user_id=None, session=None, trace_id=None, log_payloads=False,
    tone_mode=None,   # ← Addendum 1 (#23-C)
) -> RunResult[T]
```

- `timeout` 기본 8.0 동결. `fallback` 은 항상 동일 `schema` 로 환원.
- 호출처는 위 시그니처 외 인자를 추가하지 않는다. **새 파라미터는 기본값 있는 keyword-only
  로만, 하위호환을 깨지 않게** 추가한다(이것이 Addendum 의 형태).

### §2. fallback 계약

provider 미가용/timeout/검증실패/예산초과/금지어 → 즉시 `fallback` 으로 분기하고
`RunResult.fell_back=True`. 호출처는 LLM 성공/실패를 구분하지 않고 동일 schema 를 받는다.

## Addendum 1 — `tone_mode` (#23-C, 2026-06-23)

DevBaseline §부록 D Q8 잠금: 톤 모드(gentle/strict/encouraging)를 **시스템 프롬프트 prefix
1줄**로만 분기한다. 이를 동결 시그니처를 깨지 않고 싣기 위해:

- `run()` 에 **선택 keyword-only `tone_mode: str | None = None`** 추가.
- 프롬프트 렌더 직후 `prompt_text = compose_system_prompt(prompt_text, tone_mode)`
  (`llm/prompt_compose.py`, 순수 함수). `tone_mode` 가 None/미지원이면 원문 그대로 = 기존 동작.
- prefix 는 provider 전송 프롬프트에만 영향. 금지어 후처리·예산·로깅 경로는 불변.

### 배선 현황

| 호출처 | tone 전달 | 상태 |
| --- | --- | --- |
| `inbox` 라우트 | `user.tone_mode` | ✅ #23-C |
| `recovery` 라우트 | `user.tone_mode` | ✅ #23-C |
| `morning_brief` cron | `tone_mode` 파라미터(#24 wrapper 가 주입) | ✅ #23-C |
| `interview` 노드(3) | LangGraph state `tone_mode` | 🚧 후속(에이전트 코어) |
| `first_plan` 노드(2) | LangGraph state `tone_mode` | 🚧 후속(에이전트 코어) |

## 결과

- 톤은 동결 게이트 한 곳에서만 합성 → 호출처는 `tone_mode=` 한 줄만 전달.
- LangGraph(interview/first_plan)는 그래프 state 에 `tone_mode` 를 실어야 하므로 별도 슬라이스로
  분리(소유: AI/LangGraph 파트). 그 전까지 해당 경로는 톤 미적용(=기존 동작, 회귀 없음).
