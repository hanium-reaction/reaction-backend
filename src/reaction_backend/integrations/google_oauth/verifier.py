"""Google id_token 검증 — Issue #16.

- staging/prod: `google-auth` 가 id_token 의 서명 + `iss` + `aud` + `exp` 를 한 번에 검증.
- local: `AUTH_STUB_MODE=true` 시 Google 호출 우회하고 demo 클레임 반환.
  - 기본: 고정 demo 계정 (시드 시나리오 계정과 매칭).
  - `id_token="demo:<id>"`: 브라우저별 격리 데모 계정 — staging 데모에서 테스터
    전원이 한 계정을 공유하며 인터뷰 세션/동시성 lock 이 충돌하는 문제를 푼다.
    FE 는 localStorage 에 랜덤 id 를 저장해 `demo:<id>` 로 보내면 된다.
"""

from __future__ import annotations

import re
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

# 브라우저별 데모 계정 opt-in 접두사. 그 외 토큰은 전부 고정 계정(하위호환).
_STUB_DEVICE_PREFIX = "demo:"
_STUB_SLUG_STRIP = re.compile(r"[^a-z0-9_-]")


def _stub_claims(token: str) -> GoogleClaims:
    """stub 모드 클레임 결정.

    - `demo:<id>` → id 를 slug 로 정규화해 격리된 데모 계정 클레임 생성.
    - 그 외("stub" 등 기존 값 포함) → 고정 demo 계정 — 시드 데이터 계정 유지.
    """
    if token.startswith(_STUB_DEVICE_PREFIX):
        slug = _STUB_SLUG_STRIP.sub("", token[len(_STUB_DEVICE_PREFIX) :].lower())[:32]
        if slug:
            return GoogleClaims(
                sub=f"google-demo-{slug}",
                email=f"demo+{slug}@reaction.local",
                name=f"데모 {slug[:8]}",
            )
    return _STUB_CLAIMS


def verify_google_id_token(token: str) -> GoogleClaims:
    """id_token 을 검증하고 클레임을 반환한다.

    Raises:
        ApiError(AUTH_INVALID_ID_TOKEN, 401): 서명/만료/aud 불일치/형식 오류.
        RuntimeError: CLIENT_ID 미설정 + stub mode 도 꺼진 misconfig.
    """
    cfg = get_settings()

    if cfg.auth_stub_mode:
        return _stub_claims(token)

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
