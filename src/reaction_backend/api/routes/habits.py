"""Habits — 반복 행동.

규칙:
- frequency_per_week (주간 빈도) — habit_instances 생성 시 스냅샷
- 3주 연속 미달 시 빈도 재설계 제안 (Habit Penalty, S22)
- habit_instances는 매주 월요일 cron으로 자동 생성

DB: habits, habit_instances

예정 endpoint:
- GET    /habits                       — 내 습관 전체
- POST   /habits                       — 신규 습관 (frequency_per_week)
- PATCH  /habits/{id}                  — 빈도/제목 수정
- DELETE /habits/{id}                  — soft delete
- GET    /habit-instances?week=...     — 이번 주 인스턴스 (done_count vs target_count)
- POST   /habit-instances/{id}/check   — 1회 달성 카운트

구현 위치: Issue #2(DB) + scheduler/weekly_habit_generator.py.
"""

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/habits", tags=["habits"])


@router.get("", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def list_my_habits() -> None:
    """내 습관 전체."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §7 — to be implemented in a follow-up.",
    )
