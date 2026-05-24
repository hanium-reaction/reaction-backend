"""Google id_token 검증 — Issue #16.

- staging/prod: `google-auth` 가 id_token 의 서명 + `iss` + `aud` + `exp` 를 한 번에 검증.
- local: `AUTH_STUB_MODE=true` 시 Google 호출 우회하고 고정 demo 클레임 반환.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from google.auth.transport import requests as g_requests
from google.oauth2 import id_token as g_id_token

from reaction_backend.config import get_settings
from reaction_backend.schemas.errors import ApiError, ErrorCode


@dataclass(frozen=True, slots=True)
class GoogleClaims:
    """검증된 Google id_token 의 핵심 클레임."""

    sub: str  # Google account ID (안정 식별자)
    email: str
    name: str


# stub 모드용 고정 클레임 — DEMO_USER 와 email 매칭.
_STUB_CLAIMS = GoogleClaims(
    sub="google-demo-sub",
    email="demo@reaction.local",
    name="김민수",
)


def verify_google_id_token(token: str) -> GoogleClaims:
    """id_token 을 검증하고 클레임을 반환한다.

    Raises:
        ApiError(AUTH_INVALID_ID_TOKEN, 401): 서명/만료/aud 불일치/형식 오류.
        RuntimeError: CLIENT_ID 미설정 + stub mode 도 꺼진 misconfig.
    """
    cfg = get_settings()

    if cfg.auth_stub_mode:
        return _STUB_CLAIMS

    if not cfg.google_oauth_client_id:
        raise RuntimeError(
            "GOOGLE_OAUTH_CLIENT_ID is not configured. "
            "Set it, or enable AUTH_STUB_MODE for local development."
        )

    try:
        # google-auth 함수가 py.typed 미배포 — mypy strict 에서 no-untyped-call 발생.
        info: dict[str, Any] = g_id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            token,
            g_requests.Request(),
            audience=cfg.google_oauth_client_id,
        )
    except ValueError as e:
        raise ApiError(
            ErrorCode.AUTH_INVALID_ID_TOKEN,
            "Google 로그인 토큰이 유효하지 않습니다.",
            http_status=HTTPStatus.UNAUTHORIZED,
        ) from e

    sub = info.get("sub")
    email = info.get("email")
    name = info.get("name") or info.get("given_name") or ""
    if not isinstance(sub, str) or not sub or not isinstance(email, str) or not email:
        raise ApiError(
            ErrorCode.AUTH_INVALID_ID_TOKEN,
            "Google 토큰에 필요한 클레임이 없습니다.",
            http_status=HTTPStatus.UNAUTHORIZED,
        )
    return GoogleClaims(sub=sub, email=email, name=str(name))
