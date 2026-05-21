"""Settings / Privacy — S23, S28.

규칙:
- 90일 비활성 자동 익명화 (cron 04:00 KST). last_active_at 기준.
- 사용자가 명시 익명화 요청 가능 (S28). 즉시 처리.
- 데이터 익명화 = 식별 가능 필드(이름/이메일)는 hash, 통계 집계용 행 보존.
- 데이터 export (S29)는 Phase 2로 미룸 (DevBaseline §5.2).

DB: users (anonymized_at 컬럼), behavioral_profiles, interaction_styles

예정 endpoint:
- GET   /settings                  — 내 설정 메타
- PATCH /settings/tone-mode        — gentle/strict/encouraging 톤 변경
- POST  /settings/anonymize        — 즉시 익명화 요청 (확인 2단계)
- GET   /privacy/consent           — 동의 기록
- POST  /privacy/consent           — 신규 동의

구현 위치: domain/users/ + scheduler/anonymize_cron.py
"""

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def get_settings() -> None:
    """내 설정 메타."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §16 — to be implemented in a follow-up.",
    )
