"""Goals — Focus/Maintain/Parked 3 tier 목표.

규칙:
- Focus 최대 3개, Maintain 최대 5개, Parked 무제한
- deadline 있는 경우 계획 horizon 계산에 사용 (가장 먼 focus deadline까지)
- goal_nodes로 만다라트 분해 가능
- soft delete only (archived_at)

DB: goals, goal_nodes, dependency_links

예정 endpoint:
- GET    /goals                        — 내 목표 전체 (tier별 그룹)
- POST   /goals                        — 신규 목표 (tier, deadline, why_now 포함)
- PATCH  /goals/{id}                   — 제목/마감/우선순위 수정
- POST   /goals/{id}/decompose         — Goal Structuring Agent 호출 → goal_nodes 생성
- POST   /goals/{id}/park              — Focus → Parked
- DELETE /goals/{id}                   — soft delete

구현 위치: agents/goal_structuring_agent.py (Issue #5) + Issue #2.
"""

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/goals", tags=["goals"])


@router.get("", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def list_my_goals() -> None:
    """내 목표 목록 (Focus/Maintain/Parked tier별)."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §6 — to be implemented in a follow-up.",
    )
