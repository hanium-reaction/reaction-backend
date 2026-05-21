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

### `interview.py` — 모호함 0 루프
```
INIT → ASK_NEXT_SLOT ⇄ RECEIVE_ANSWER → UPDATE_AMBIGUITY
                                            ↓
                                  AMBIGUITY=0 / 15턴 / [충분해요] → DONE
```

> Orchestrator는 **LLM 호출을 직접 하지 않는다**. 모든 LLM 작업은 agents/를 거친다.
