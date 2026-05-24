# ADR 0005 — Agentic Architecture for Alpha MVP (LangGraph 채택)

| 항목 | 내용 |
| --- | --- |
| Status | **Accepted** |
| Date | 2026-05-24 |
| Deciders | PM Mbt70 (단독 결정, 팀원 요청에 따른 설계) |
| Reviewers | @peterchopg (AI) · @hyeongjun22 (BE) · @choigod1023 (FE) |
| Supersedes | — |
| Related | ADR-0003 (LLM Tool Executor 시그니처 동결, PR #33) · 베이스라인 §부록 D Q9 |

---

## 1. Context

Issue #6 (Deep Interview), Issue #20 (Recovery), Issue #32 (Planning LLM 통합) 등 5 AI 모듈 본격 구현 직전이다. 베이스라인 §부록 D Q9 ("LangGraph 채택 여부 — AI 파트 1주 PoC 후 결정") 가 미해결 상태로 남으면 각 Agent 가 제각각의 패턴으로 구현되어 통합 리뷰가 불가능해진다.

### 1.1 결정해야 할 이유
- 5 Agent (Interview / Planning / Brief / Recovery / Inbox Parser) 가 **각자 다른 orchestration 패턴**으로 구현되면 디버깅·튜닝·확장 모두 어려워짐
- 베이스라인 §1.4 잠금 결정 다수 (HITL 게이트 / 8s timeout / 룰 fallback / Draft Layer) 를 5 Agent 가 일관되게 준수해야 함
- 2주 MVP 일정상 "1주 PoC 후 결정" 절차를 따를 시간 부족 → PM 단독 결정 + 팀 사후 추인 절차로 진행

### 1.2 현재 코드 전제
- PR #33 (Issue #5 LLM Infra) — `aiClient.run(module, schema, prompt_id, fallback, timeout=8.0)` 단일 게이트 동결 (ADR-0003). 본 ADR 은 **PR #33 머지를 전제**로 한다.
- PR #30 (Issue #18 룰 부분) — `orchestrator/goal_structuring.py` 룰 기반 스케줄러 (Planning Agent 의 fallback 으로 활용 예정).
- PR #34 (Issue #16 Auth) — `get_current_user` 의존성 + JWT 세션. 모든 Agent 호출은 인증 필수.

---

## 2. Decision

**LangGraph 채택** + **PR #33 `aiClient.run(...)` 단일 게이트를 LangGraph Node 내에서 직접 사용** + **베이스라인 §12.1 5개 Agent 분리**.

### 2.1 8개 영역 결정 요약

| # | 영역 | 결정 | 근거 |
| --- | --- | --- | --- |
| 1 | **프레임워크** | **LangGraph** (`langgraph >= 0.2.x`) | State + Node + Edge 가 베이스라인 §6 "슬롯 채우기 + 모호함 0 까지 cycle" 과 직접 매핑. 상태 시각화 (Mermaid) 가 시연 자료 + 디버깅에 강점. |
| 2 | **LangChain 의존성 범위** | `langchain-core` 만 (= langgraph 의 transitive dep). `langchain` / `langchain-openai` 등 **전체 ecosystem 금지** | 의존성 무게 최소화, 보안 surface 최소화 |
| 3 | **LLM 호출 경로** | LangGraph Node 안에서 `aiClient.run(...)` **직접 호출**. LangChain ChatModel wrapping 금지 | AGENTS.md §2 "LLM SDK 직접 import 금지" 룰 유지. PR #33 의 budget · banned words · llm_runs 로깅 일관 적용 |
| 4 | **Agent 분리 수준** | **5 Agent** (Interview / Planning / Brief / Recovery / Inbox Parser) + **룰 sub-helper** (Validation · Review · Failure Diagnosis · Scheduler) | 베이스라인 §12.1 그대로. 9 Agent 세분화는 Phase 3 |
| 5 | **Orchestrator 패턴** | Interview: **Cyclic StateGraph** · Recovery: **Conditional StateGraph** · Brief / Inbox: **Sequential** · Planning: **Sequential + 룰 fallback** | architecture.md §2 명시된 상태머신 그대로 |
| 6 | **State 관리** | **DB-backed** (`interview_sessions`, `recovery_attempts` 등 기존 모델) + LangGraph `MemorySaver` (단일 요청 내 short-lived) | 재진입 가능 + 디버깅 용이. Redis 는 P2 |
| 7 | **Tool Calling** | **Structured Output (Pydantic schema)** — PR #33 현재 그대로 | Function calling 변경 시 fallback 룰 복잡도 ↑ |
| 8 | **Observability** | `llm_runs` 자체 로깅 (PR #33) + 매주 30분 "오류 잔치" (베이스라인 §12.4) | LangSmith / LangFuse 는 Phase 2 |

### 2.2 시스템 다이어그램

```
┌─────────────────────────────────────────────────────────────────┐
│ FastAPI Routers (PR #34 인증 + ADR-0002 envelope)               │
│   ↳ interview / planning / today / reflection / recovery / ... │
└─────────────────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│ Orchestrators (LangGraph StateGraph) ⭐ 본 ADR                  │
│   • interview/graph.py      (Cyclic)                            │
│   • recovery/graph.py       (Conditional, 룰 fallback 3종)      │
│   • planning/graph.py       (Sequential, LLM 4 + 룰)            │
│   • brief/graph.py          (Sequential 단순)                   │
│   • inbox_parser/graph.py   (Sequential 단순)                   │
└─────────────────────────────────────────────────────────────────┘
                          ▼ (Node 내부)
┌─────────────────────────────────────────────────────────────────┐
│ LLM Tool Executor — aiClient.run(...) (ADR-0003, PR #33)        │
│   • Structured Output · prompts/registry · safety/banned_words  │
│   • llm_budget.check/record · timeout 8s · 룰 fallback          │
└─────────────────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│ Gemini API (격리, AGENTS.md §2)                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 2.3 Interview Agent 예시 코드 (Cyclic StateGraph)

본 예시는 팀원이 다른 Agent 작성 시 참고할 **canonical pattern**.

```python
# src/reaction_backend/orchestrator/interview/graph.py
from typing import TypedDict
from uuid import UUID

from langgraph.graph import StateGraph, END

from reaction_backend.llm.tool_executor import aiClient
from reaction_backend.schemas.interview import NextQuestionSchema, AmbiguityUpdate


class InterviewState(TypedDict):
    """LangGraph 가 Node 간 전달하는 상태. DB 와 별도 (short-lived)."""
    session_id: UUID
    ambiguity_score: int
    total_turns: int
    last_answer: str | None
    next_question: NextQuestionSchema | None
    early_finish: bool  # 사용자 [충분해요] 탭


async def ask_next_slot(state: InterviewState) -> InterviewState:
    """LLM 호출 — 다음 질문 생성. PR #33 aiClient.run 그대로 사용."""
    result = await aiClient.run(
        module="interview",
        schema=NextQuestionSchema,
        prompt_id="interview/next_question",
        fallback=rule_based_next_question,  # 8s timeout 시 룰 fallback
        timeout=8.0,
        variables={"current_ambiguity": str(state["ambiguity_score"])},
    )
    return {**state, "next_question": result.value, "total_turns": state["total_turns"] + 1}


async def receive_answer(state: InterviewState) -> InterviewState:
    """사용자 답 수신 (라우터에서 주입). DB 업데이트는 별도."""
    # 이 노드는 외부 트리거 (POST /interview/sessions/{id}/answers) 로 진입
    return state


async def update_ambiguity(state: InterviewState) -> InterviewState:
    """clarity 채점 + 모호함 지표 갱신. LLM 호출 — 답 정규화 포함."""
    result = await aiClient.run(
        module="interview",
        schema=AmbiguityUpdate,
        prompt_id="interview/ambiguity_score",
        fallback=heuristic_ambiguity_update,
        timeout=8.0,
        variables={"answer": state["last_answer"] or ""},
    )
    return {**state, "ambiguity_score": result.value.new_score}


def should_continue(state: InterviewState) -> str:
    """Cycle 종료 조건. 베이스라인 §2.5 핵심."""
    if state["ambiguity_score"] == 0:
        return END
    if state["total_turns"] >= 15:  # 베이스라인 §6 최대 15턴
        return END
    if state["early_finish"]:
        return END
    return "ask_next_slot"


def build_interview_graph() -> StateGraph:
    graph = StateGraph(InterviewState)
    graph.add_node("ask_next_slot", ask_next_slot)
    graph.add_node("receive_answer", receive_answer)
    graph.add_node("update_ambiguity", update_ambiguity)

    graph.set_entry_point("ask_next_slot")
    graph.add_edge("ask_next_slot", "receive_answer")
    graph.add_edge("receive_answer", "update_ambiguity")
    graph.add_conditional_edges("update_ambiguity", should_continue)

    return graph.compile()
```

### 2.4 Recovery Agent 예시 (Conditional)

```python
# src/reaction_backend/orchestrator/recovery/graph.py
from langgraph.graph import StateGraph, END

async def diagnose_failure(state):
    """LLM ⑤ Failure Diagnosis. fallback: 룰 (failure_tag → strategy)"""
    ...

async def generate_proposals(state):
    """LLM ⑥ Recovery Coach. fallback: 룰 3종 (PR #30 재사용)"""
    ...

async def heuristic_fallback(state):
    """베이스라인 §부록 C — plan_too_big → downscope 등 룰 매핑."""
    ...

def should_use_fallback(state) -> str:
    """8s timeout 또는 LLM 실패 시 룰 분기."""
    return "heuristic_fallback" if state.get("llm_failed") else "generate_proposals"

def build_recovery_graph() -> StateGraph:
    graph = StateGraph(RecoveryState)
    graph.add_node("diagnose_failure", diagnose_failure)
    graph.add_node("generate_proposals", generate_proposals)
    graph.add_node("heuristic_fallback", heuristic_fallback)

    graph.set_entry_point("diagnose_failure")
    graph.add_conditional_edges("diagnose_failure", should_use_fallback)
    graph.add_edge("generate_proposals", END)
    graph.add_edge("heuristic_fallback", END)
    return graph.compile()
```

---

## 3. Consequences

### 3.1 긍정
- **시연 차별점**: `graph.get_graph().draw_mermaid()` 로 상태머신 시각화 → 한이음 발표 자료에 그대로 사용
- **Cycle / Conditional 표현이 명시적** → Interview "모호함 0 까지" 루프, Recovery "룰 fallback 3종" 분기가 코드에서 한눈에 보임
- **Phase 2/3 마이그레이션 path 보존** — 베이스라인 §13.3 "Coach + Planner + Reviewer 분리"가 StateGraph 노드 추가로 자연스러움
- **PR #33 단일 게이트 그대로 유지** — budget · banned words · llm_runs 로깅 일관성 보존
- **베이스라인 §1.4 잠금 결정 일관 enforce** — Draft Layer / 3버튼 / HITL 게이트가 graph 종료 노드에서 자연스럽게 표현

### 3.2 부정
- **peterchopg 학습 곡선 1~3일** — LangGraph 첫 접촉 시. 영문 docs 위주
- **`langgraph` + `langchain-core` 의존성 추가** — 약 8MB, 보안 surface ↑
- **LangChain ecosystem 일부 호환성 제약** — 우리가 사용하지 않는 `langchain-openai` 등의 breaking change 가 transitive 로 영향 줄 수 있음 → `pyproject.toml` 에 명시적 version pin

### 3.3 위험 완화
- **첫 Agent (Interview) PoC 후 패턴 검증** — PM 이 직접 PR 리뷰
- **시간 초과 시 fallback path**: 자체 FSM 으로 회귀 가능 (PR #33 `aiClient.run(...)` 만 그대로 두면 됨)
- **학습 자료 박제** (본 ADR §6) — 팀원이 따라올 수 있는 canonical pattern + 영상/문서 링크

---

## 4. Implementation Roadmap

각 Issue 의 PR 본문에 본 ADR-0005 를 reference 로 박제.

| 단계 | Issue | Agent | 패턴 | 주 담당 |
| --- | --- | --- | --- | --- |
| 1 | `#6` Deep Interview ⭐ | Interview | **Cyclic** (canonical PoC) | peterchopg |
| 2 | `#32` Planning LLM 통합 | Planning | Sequential + 룰 fallback (PR #30 재사용) | peterchopg |
| 3 | `#19` Today / Brief | Brief | Sequential | peterchopg + hyeongjun22 |
| 4 | `#20` Recovery ⭐ | Recovery | Conditional (룰 fallback 3종) | peterchopg + hyeongjun22 |
| 5 | `#22` Inbox Parser (Inbox 일부) | Inbox Parser | Sequential | peterchopg + hyeongjun22 |
| (선택) | `#23` Habit Penalty | Habit Penalty | Sequential | hyeongjun22 |

**우선순위**: Interview (#6) 가 가장 먼저. Cyclic 패턴이 가장 복잡 → 이게 검증되면 나머지 4개는 빠르게 채울 수 있다.

### 4.1 의존성 추가 PR (peterchopg 작업)

```bash
uv add 'langgraph>=0.2,<0.3'
# pyproject.toml + uv.lock 함께 커밋
```

`pyproject.toml` 명시 (현재):
```toml
[project]
dependencies = [
    # ... 기존 ...
    "langgraph>=0.2,<0.3",  # ADR-0005 — Agentic Orchestration
]
```

`langchain`, `langchain-openai`, `langchain-anthropic` 등은 **추가 금지**.

---

## 5. Open Questions (다음 결정)

- **LangSmith / LangFuse 도입 시점** — Phase 2 (베타 안정화 후). 일단 `llm_runs` 자체 로깅으로 충분
- **Multi-model fallback** (Gemini 외 OpenAI 백업) — Phase 3. 현재는 룰 fallback 으로 충분
- **Streaming (SSE 토큰)** — Interview 다음 질문 typing 효과는 P1 후속 PR. MVP 는 전체 응답 대기 + 8s timeout
- **Graph state 영속화** (`langgraph.checkpoint`) — DB 모델로 이미 처리. langgraph checkpoint 도입 여부 Phase 2

---

## 6. References / 학습 자료

### 6.1 LangGraph 공식
- 튜토리얼: https://langchain-ai.github.io/langgraph/tutorials/
- API Reference: https://langchain-ai.github.io/langgraph/reference/graphs/
- StateGraph 패턴: https://langchain-ai.github.io/langgraph/concepts/low_level/
- Conditional Edges: https://langchain-ai.github.io/langgraph/how-tos/branching/

### 6.2 우리 프로젝트 연관 문서
- `docs/api-contract.md` — 라우터가 Orchestrator 를 어떻게 호출하는지
- `docs/architecture.md` §2 — Orchestrator 3 종 상태머신 (LangGraph 도입 전 안)
- `docs/decisions/0003-llm-tool-executor.md` (PR #33) — `aiClient.run(...)` 시그니처 동결
- 베이스라인 §6 — Deep Interview 슬롯 채우기 cycle 명세
- 베이스라인 §12 — AI/멀티에이전트 가이드
- 베이스라인 §부록 C — Recovery 룰 fallback 매핑

### 6.3 추천 학습 순서 (peterchopg 1일)
1. 30분 — StateGraph 기본 개념 (TypedDict state, add_node, add_edge)
2. 1시간 — Conditional Edges + Cycle (interview 패턴 직접 코딩 해보기)
3. 30분 — `graph.compile()` + `await graph.ainvoke(initial_state)` 실행 패턴
4. 1~2시간 — 본 ADR §2.3 Interview 예시 코드를 `prompts/interview/next_question.v1.md` (PR #33) 와 연결해 동작 확인
5. (선택) `graph.get_graph().draw_mermaid()` 로 시각화 → 시연 자료에 박제

---

## 7. 변경 절차

본 ADR 의 결정을 바꾸려면:
1. 새 ADR (0006+) 발행
2. peterchopg + hyeongjun22 + PM 합의
3. 기존 ADR Status 를 `Superseded by 0006` 으로 변경 (별도 PR)

**LangGraph 자체 회귀 시나리오** (학습 곡선 / 안정성 문제 발견 시):
- `aiClient.run(...)` 는 그대로 유지 → Orchestrator 만 자체 FSM 으로 교체
- Agent 별로 점진 교체 가능 (Big bang X)
