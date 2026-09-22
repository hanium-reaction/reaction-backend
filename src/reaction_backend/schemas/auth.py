"""Auth 도메인 스키마 (api-contract §2) — S01 Welcome.

#3-B 단계는 mock 스텁. 실제 Google OAuth·JWT 발급/검증은 #16.
"""

from __future__ import annotations

from pydantic import Field

from reaction_backend.schemas.common import CamelModel
from reaction_backend.schemas.settings import ToneMode


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
    """사용자 프로필 — GET /auth/me 및 로그인 응답에 포함.

    `tone_mode` 는 아직 톤을 고르지 않은 사용자(인터뷰 전)에서 **null**. 예전엔 여기서만
    빈 문자열로 내려 `GET /settings` 의 같은 사용자 같은 값이 `null` 과 `""` 로 갈렸다
    (재검증 P4). 빈 문자열은 "고르지 않음"이 아니라 "고른 값이 비어 있음"처럼 읽히는
    거짓말이고, 두 화면이 같은 사람을 다르게 말하면 그걸 읽는 쪽이 둘 다 방어해야 한다.
    """

    user_id: str
    email: str
    name: str
    timezone: str
    onboarding_state: str
    tone_mode: ToneMode | None


class AuthSession(CamelModel):
    """POST /auth/google 응답 — 토큰 쌍 + 사용자."""

    access_token: str
    refresh_token: str
    user: UserProfile


class AccessToken(CamelModel):
    """POST /auth/refresh 응답."""

    access_token: str
