"""Auth 도메인 스키마 (api-contract §2) — S01 Welcome.

#3-B 단계는 mock 스텁. 실제 Google OAuth·JWT 발급/검증은 #16.
"""

from __future__ import annotations

from pydantic import Field

from reaction_backend.schemas.common import CamelModel


class GoogleLoginRequest(CamelModel):
    """POST /auth/google 요청 — Google id_token (+ 신규 가입만 `inviteCode` 필요, #324)."""

    id_token: str = Field(min_length=1, description="Google OAuth id_token")
    invite_code: str | None = Field(
        default=None,
        max_length=32,
        description="신규 가입에만 필요. 기존 사용자 로그인은 무시된다.",
    )


class RefreshRequest(CamelModel):
    """POST /auth/refresh 요청.

    `refreshToken` 생략 가능(#323) — 웹은 `reaction_refresh` httpOnly 쿠키로 대신 보낼 수
    있다(로그인 시 서버가 body 와 쿠키에 **둘 다** 내려준다, 이행 기간). 본문·쿠키 둘 다
    없으면 401 `AUTH_INVALID_TOKEN`.
    """

    refresh_token: str | None = Field(default=None, min_length=1)


class LogoutRequest(CamelModel):
    """POST /auth/logout 요청. `refreshToken` 생략 가능 — `RefreshRequest` 와 동일한 이유(#323)."""

    refresh_token: str | None = Field(default=None, min_length=1)


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
