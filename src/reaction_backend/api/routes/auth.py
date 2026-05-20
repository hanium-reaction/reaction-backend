"""Auth — Google OAuth 로그인 + JWT 세션.

화면: S01 Welcome
DB: users, (refresh token 저장 위치는 Issue #2에서 결정)

예정 endpoint (api-contract.md §2):
- POST /auth/google         — Google id_token 검증 → JWT access/refresh 발급
- POST /auth/refresh        — refresh token → 새 access token
- POST /auth/logout         — refresh token 무효화
- GET  /auth/me             — 현재 사용자 (onboarding_state 포함)

구현: Issue #1 직접 후속 (Auth) 또는 Issue #3 (Backend API Contract v0)에서 진행.
"""

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/google", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def login_with_google() -> None:
    """Google id_token 검증 후 자체 JWT 발급."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §2 — to be implemented in a follow-up.",
    )
