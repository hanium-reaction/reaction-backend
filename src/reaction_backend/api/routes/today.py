"""Today / Execution — S10, S11, S12, S13.

흐름:
  S10 (Today Agenda) → 카드 탭 → S11 → [▶ 시작] → S12 (Focus Entry)
  → S13 (Focus Timer) → Quick Check-in 4칩 (완료/일부만/잘안됨/넘침)

핵심 데이터 출처:
- daily_briefs (v0.7, 06시 cron precompute) — 헤드라인 + Big Rock + adjustmentHints
- action_items, scheduled_blocks, habit_instances, fixed_schedules
- execution_events (Quick Check-in 시 status, actualDuration, userRating, userFeedback 저장)
- interruption_events (v0.6, 일시정지/재개 컨텍스트)
- context_snapshots (v0.6, 14 필드 — 시간/요일/에너지/방해 등)

DB: action_items, scheduled_blocks, execution_events, interruption_events, context_snapshots,
    daily_briefs, focus_sessions (선택)

예정 endpoint:
- GET   /today/agenda                          — S10 진입 시 단일 조회
- GET   /today/actions/{id}                    — S11 카드 상세
- POST  /today/actions/{id}/start              — [▶ 시작] (S12)
- POST  /today/focus/{exec_id}/pause           — [⏸] (S13) + interruption_events INSERT
- POST  /today/focus/{exec_id}/resume          — [▶ 계속]
- POST  /today/check-ins                       — Quick Check-in 4칩 + context_snapshot

구현 위치: agents/execution_logger_agent.py + scheduler/daily_brief_precompute.py
"""

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/today", tags=["today"])


@router.get("/agenda", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def today_agenda() -> None:
    """오늘 어젠다 — daily_brief + 카드 + 습관 + 고정 일정 단일 조회."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §10 — to be implemented in a follow-up.",
    )
