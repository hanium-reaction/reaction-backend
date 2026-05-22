"""Auth — Google OAuth 로그인 + JWT 세션 (S01, api-contract §2).

#3-B 단계는 **mock 스텁**: 고정 demo user + 가짜 토큰을 반환한다.
실제 Google id_token 검증·JWT 발급/회전·refresh 저장은 #16 에서 구현.
인증 미들웨어는 Issue #3 범위 밖 — `/auth/me` 는 토큰 검사 없이 demo user 를 반환.
"""

from fastapi import APIRouter, status

from reaction_backend.api.mock.demo import DEMO_ACCESS_TOKEN, DEMO_REFRESH_TOKEN, DEMO_USER
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


def _demo_profile() -> UserProfile:
    """고정 demo user 를 API 프로필 형태로 변환."""
    return UserProfile(
        user_id=DEMO_USER.id,
        email=DEMO_USER.email,
        name=DEMO_USER.name,
        timezone=DEMO_USER.timezone,
        onboarding_state=DEMO_USER.onboarding_state,
        tone_mode=DEMO_USER.tone_mode,
    )


@router.post("/google")
async def login_with_google(body: GoogleLoginRequest) -> AuthSession:
    """[stub] Google id_token → 자체 JWT 발급. 실제 검증은 #16."""
    return AuthSession(
        access_token=DEMO_ACCESS_TOKEN,
        refresh_token=DEMO_REFRESH_TOKEN,
        user=_demo_profile(),
    )


@router.post("/refresh")
async def refresh_access_token(body: RefreshRequest) -> AccessToken:
    """[stub] refresh token → 새 access token. demo refresh token 만 유효하게 취급."""
    if body.refresh_token != DEMO_REFRESH_TOKEN:
        raise ApiError(
            ErrorCode.AUTH_INVALID_TOKEN,
            "refresh token 이 유효하지 않아요. 다시 로그인해 주세요.",
            http_status=status.HTTP_401_UNAUTHORIZED,
        )
    return AccessToken(access_token=DEMO_ACCESS_TOKEN)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: LogoutRequest) -> None:
    """[stub] refresh token 무효화."""
    return None


@router.get("/me")
async def get_current_user() -> UserProfile:
    """[stub] 현재 사용자 (onboarding_state 포함). 인증 검사는 #16."""
    return _demo_profile()
