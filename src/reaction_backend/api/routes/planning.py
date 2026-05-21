"""Planning — Weekly plan (S06, S14, S15, S16).

핵심 흐름 (Orchestrator 1 — Goal Structuring):
  VALIDATING → PLANNING → REVIEWING → HITL → SAVING

규칙:
- 입력: time_policies + goals + habits + behavioral_profiles + interview + calendar freebusy
- horizon = focus goals의 가장 먼 deadline
- 출력: action_items + scheduled_blocks + dependency_links + habit_instances
- 정책 위반(수면/점심 슬롯) 블록 생성 시 트랜잭션 롤백
- 모든 변경은 사용자 [승인] 후 적용 (Draft Layer)

DB: action_items, scheduled_blocks, dependency_links, llm_runs

예정 endpoint:
- POST  /plans/generate                 — 첫 계획 또는 재생성 (S06)
- GET   /plans/{plan_id}                — 미리보기 (workload, conflicts 포함)
- POST  /plans/{plan_id}/approve        — 사용자 승인 → 활성화
- PATCH /plans/{plan_id}/blocks/{id}    — 직접 편집 (S15, 15분 snap)
- POST  /plans/{plan_id}/ai-edit        — 자연어 수정 (S16, P1)
- GET   /plans/weekly?week=...          — 주간 그리드 데이터 (S14)

구현 위치: agents/{validation,planning,scheduler,review}_agent.py + orchestrator/goal_structuring.py
"""

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/plans", tags=["planning"])


@router.post("/generate", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def generate_plan() -> None:
    """주간/horizon 계획 생성 — Goal Structuring orchestrator 실행."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §8 — to be implemented in a follow-up.",
    )
