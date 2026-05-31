# `orchestrator/` — 상태머신 오케스트레이터

LLM을 직접 호출하지 않는 **순수 상태머신**. Worker Agent를 호출·검증·라우팅한다.

후속 이슈(#5)에서 추가될 모듈:

### `goal_structuring.py` — Orchestrator 1
```
START → VALIDATING → PLANNING → REVIEWING → HITL → SAVING → DONE
                ↑__________ feedback ____________│
```
- Validation/Planning/Review Agent 호출
- DB Agent (단일 트랜잭션) 호출
- 정책 위반 감지 시 즉시 롤백

### `recovery.py` — Orchestrator 2
```
START → DETECTING → DIAGNOSING → COACHING → HITL → UPDATING → SAVING → DONE
```
- Detection Agent (룰 기반) → Failure Diagnosis → Recovery Coach
- 8초 타임아웃, 룰 폴백
- recovery_attempts INSERT (후보별), 사용자 결정 후 UPDATE

### `interview.py` — Rule-based Slot FSM (#6 실구현)
```
INIT → ask_question ⇄ receive_answer → validate_answer
                                            ↓ should_continue
   필수 슬롯 완료 / ambiguity≤0.2 / 15턴 / [충분해요] / 3턴 정체
                                            ↓ finish
                       summarize_interview → finalize_outcome(InterviewOutcome) → DONE
```
- **다음 슬롯 선택·종료 판정은 순수 룰** (`_next_required_slot` / `_terminal_reason`).
  문장 생성·답 채점만 `aiClient.run(... timeout=8.0, fallback=룰)`. 8s timeout 이 와도
  카탈로그 기본 질문/룰 채점으로 회귀해 흐름이 끊기지 않는다.
- 노드: `ask_question`(질문) → `receive_answer`(외부 답 주입 지점, no-op) →
  `validate_answer`(채점·정규화·슬롯 저장) → `summarize_interview`(요약 확인 카드) →
  `finalize_outcome`(LLM 0회로 경계 계약 `InterviewOutcome` 빌드).
- 라우터는 그래프를 한 번에 ainvoke 하지 않고 **`interview_runner`** 로 턴 단위 구동한다
  (사용자 답이 매 HTTP 요청으로 들어오므로). `start_interview` / `submit_and_advance` /
  `finish_early` 가 진입점.

### `interview_runner.py` — 턴 드라이버 (라우터 ↔ FSM 브리지)
`InterviewState`(직렬화 가능)를 라우터가 요청 사이에 보관(영속)하고, 각 턴마다
노드를 엮어 "질문 1개" 또는 "요약 + InterviewOutcome" 을 도메인 객체로 직접 반환한다.

> Orchestrator는 **LLM 호출을 `aiClient.run()` 외 경로로 하지 않는다** (Gemini SDK 직접 import 금지).
