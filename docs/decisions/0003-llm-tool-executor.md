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
| `interview` 노드(3) | `config["configurable"]["tone_mode"]` (runner 가 주입) | ✅ #23-D |
| `first_plan` 노드(2) | `config["configurable"]["tone_mode"]` (planning route 가 주입) | ✅ #23-D |

### LangGraph 전달 경로 (#23-D)

세션과 동일하게 **state 가 아닌 `config["configurable"]` 채널**로 tone 을 나른다(ADR-0005 §7.1
"비직렬화/요청-scope 값은 config 로"). state(`InterviewState`/`FirstPlanState`) 스키마는 불변:

- 노드: `tone_mode=_tone_mode(config)` (각 orchestrator 의 `_session` 옆 헬퍼).
- 주입: `interview_runner._config(session, tone_mode)` + `planning._config(session, tone_mode)`.
  runner/route 가 `user.tone_mode` 를 넘긴다.

## 결과

- 톤은 동결 게이트 한 곳에서만 합성 → 호출처는 `tone_mode=` 한 줄(또는 config 채널)만 전달.
- LangGraph 도 config 채널로 일관 처리 → state 스키마/직렬화 불변, 회귀 없음.
- 모든 LLM 호출(5도메인 8지점)에 톤 적용 완료.

## Addendum — 라우트별 timeout override (2026-07, #128)

동결된 것은 **`timeout=8.0` 기본값**이지 호출별 값이 아니다. 호출처는 작업 특성에 따라
override 할 수 있고, 현재 유일한 override 는 recovery personalize 다:

- `api/routes/recovery.py` — `thinking_budget=0` + `timeout=12.0` **한 쌍**.
- 사유(#128): flash 계열이 SDK 기본 thinking 으로 8s 를 상습 초과해 **매번 폴백**됐다
  (회복 카드가 항상 카탈로그 템플릿 = LLM personalize 무용). thinking 을 끄면 품질 손실
  없이 2~4s 로 내려오고, 12s 는 그 위의 여유다.
- 다른 도메인(inbox/brief 등)은 기본 8s 유지. 새 override 를 추가하면 이 절에 사유와 함께
  기록할 것 — 문서에 없는 override 는 리뷰에서 "8s 위반"으로 오독된다(실제로 그랬다).
