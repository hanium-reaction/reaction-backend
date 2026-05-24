"""Auth — Google OAuth 로그인 + JWT 세션 (S01, api-contract §2).

Issue #16 실구현:
- `/auth/google`  : Google id_token 검증 → users upsert → access(60m) + refresh(14d) 발급
- `/auth/refresh` : refresh → 새 access. 회전 X (MVP). revoke 된 jti 는 401.
- `/auth/logout`  : refresh 의 jti 를 revoke set 에 등록. 잘못된 토큰이어도 멱등하게 204.
- `/auth/me`      : `CurrentUser` 의존성 (api/deps.py) — Bearer JWT 검증.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.auth.jwt import (
    JwtError,
    JwtErrorReason,
    decode_token,
    issue_access_token,
    issue_refresh_token,
)
from reaction_backend.auth.revoke import RevokeStore, get_revoke_store
from reaction_backend.db.models.user import User
from reaction_backend.db.session import get_db
from reaction_backend.integrations.google_oauth.verifier import verify_google_id_token
from reaction_backend.repositories.user_repo import GoogleProfile, UserRepo, get_user_repo
from reaction_backend.schemas.auth import (
    AccessToken,
    AuthSession,
    GoogleLoginRequest,
    LogoutRequest,
    RefreshRequest,
    UserProfile,
)
from reaction_backend.schemas.errors import ApiError, ErrorCode

router = APIRouter(prefix="/auth", tags=["auth"])


def _to_profile(user: User) -> UserProfile:
    """User ORM → API UserProfile (ADR-0001 §3.1: API 식별자에 `user_` prefix).

    tone_mode 는 신규 user 에서 None 가능 — 빈 문자열로 fallback (FE 는 기본 톤).
    """
    return UserProfile(
        user_id=f"user_{user.id}",
        email=user.email,
        name=user.name,
        timezone=user.timezone,
        onboarding_state=user.onboarding_state,
        tone_mode=user.tone_mode or "",
    )


@router.post("/google")
async def login_with_google(
    body: GoogleLoginRequest,
    repo: Annotated[UserRepo, Depends(get_user_repo)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AuthSession:
    """Google id_token 검증 → user upsert → JWT 발급."""
    claims = verify_google_id_token(body.id_token)
    user = await repo.upsert_from_google(
        GoogleProfile(email=claims.email, name=claims.name),
    )
    await session.commit()

    access = issue_access_token(user.id)
    refresh = issue_refresh_token(user.id)
    return AuthSession(
        access_token=access.token,
        refresh_token=refresh.token,
        user=_to_profile(user),
    )


@router.post("/refresh")
async def refresh_access_token(
    body: RefreshRequest,
    revoke_store: Annotated[RevokeStore, Depends(get_revoke_store)],
) -> AccessToken:
    """refresh → 새 access. refresh 회전 X (refresh 자체 재발급 안 함)."""
    try:
        decoded = decode_token(body.refresh_token, expected_type="refresh")
    except JwtError as e:
        if e.reason is JwtErrorReason.EXPIRED:
            raise ApiError(
                ErrorCode.AUTH_TOKEN_EXPIRED,
                "refresh token 이 만료됐어요. 다시 로그인해 주세요.",
                http_status=HTTPStatus.UNAUTHORIZED,
            ) from e
        raise ApiError(
            ErrorCode.AUTH_INVALID_TOKEN,
            "refresh token 이 유효하지 않습니다.",
            http_status=HTTPStatus.UNAUTHORIZED,
        ) from e

    if revoke_store.is_revoked(decoded.jti):
        raise ApiError(
            ErrorCode.AUTH_INVALID_TOKEN,
            "refresh token 이 더 이상 유효하지 않습니다.",
            http_status=HTTPStatus.UNAUTHORIZED,
        )

    new_access = issue_access_token(decoded.user_id)
    return AccessToken(access_token=new_access.token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: LogoutRequest,
    revoke_store: Annotated[RevokeStore, Depends(get_revoke_store)],
) -> None:
    """refresh 의 jti 를 revoke set 에 등록. 잘못된 토큰이어도 멱등 204."""
    try:
        decoded = decode_token(body.refresh_token, expected_type="refresh")
    except JwtError:
        return None
    revoke_store.revoke(decoded.jti, decoded.expires_at)
    return None


@router.get("/me")
async def get_current_user_profile(user: CurrentUser) -> UserProfile:
    """현재 사용자 + onboardingState 반환 (`Depends(get_current_user)`)."""
    return _to_profile(user)
