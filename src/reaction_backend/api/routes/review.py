"""Review — Weekly Review (S21, S22).

핵심 KPI (Memory Structure Weekly Report 12개 항목):
- adherence_rate (DONE/total)
- consistency_days (연속 달성)
- resilience_rate (회복 후 24h 내 완료)
- average_recovery_minutes
- category_success_rate (JSONB)
- drain_point_window / peak_window
- 추천 정책 변경 (policy_update_candidates)

생성: 사용자 timezone 일요일 03:00 cron precompute.

DB: period_summaries (period_type='weekly'), interruption_events, context_snapshots,
    recovery_attempts, habits, llm_runs

예정 endpoint:
- GET  /reviews/weekly?week=...            — 이번 주 리뷰 카드 (precomputed)
- POST /reviews/weekly/generate            — 수동 재생성 (디버그/관리자)
- POST /reviews/habit-penalty/{habit_id}/accept — 3주 연속 미달 페널티 수락

구현 위치: agents/weekly_review_agent.py + scheduler/weekly_review_precompute.py
"""

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.get("/weekly", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def get_weekly_review() -> None:
    """이번 주 (또는 지정 주차) 리뷰 카드."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §13 — to be implemented in a follow-up.",
    )
