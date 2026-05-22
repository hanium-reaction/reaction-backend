"""Auth 도메인 스키마 (api-contract §2) — S01 Welcome.

#3-B 단계는 mock 스텁. 실제 Google OAuth·JWT 발급/검증은 #16.
"""

from __future__ import annotations

from pydantic import Field

from reaction_backend.schemas.common import CamelModel


class GoogleLoginRequest(CamelModel):
    """POST /auth/google 요청 — Google id_token."""

    id_token: str = Field(min_length=1, description="Google OAuth id_token")


class RefreshRequest(CamelModel):
    """POST /auth/refresh 요청."""

    refresh_token: str = Field(min_length=1)


class LogoutRequest(CamelModel):
    """POST /auth/logout 요청."""

    refresh_token: str = Field(min_length=1)


class UserProfile(CamelModel):
    """사용자 프로필 — GET /auth/me 및 로그인 응답에 포함."""

    user_id: str
    email: str
    name: str
    timezone: str
    onboarding_state: str
    tone_mode: str


class AuthSession(CamelModel):
    """POST /auth/google 응답 — 토큰 쌍 + 사용자."""

    access_token: str
    refresh_token: str
    user: UserProfile


class AccessToken(CamelModel):
    """POST /auth/refresh 응답."""

    access_token: str
