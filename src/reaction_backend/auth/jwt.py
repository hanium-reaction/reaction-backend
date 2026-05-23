"""JWT 발급 / 검증 — re:action 자체 세션 (Issue #16).

- 알고리즘: HS256 (대칭키, `JWT_SECRET`).
- access TTL = `JWT_ACCESS_TOKEN_TTL_MINUTES` (기본 60분), `type='access'`.
- refresh TTL = `JWT_REFRESH_TOKEN_TTL_DAYS` (기본 14일), `type='refresh'`.
- claims: `sub`(user_id UUID 문자열) · `iat` · `exp` · `type` · `jti`.
- refresh 회전 X (MVP, Issue #16 본문 명시). logout 무효화는 `auth.revoke` revoke store.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

import jwt as pyjwt

from reaction_backend.config import get_settings

TokenType = Literal["access", "refresh"]


class JwtErrorReason(StrEnum):
    EXPIRED = "EXPIRED"
    INVALID = "INVALID"
    WRONG_TYPE = "WRONG_TYPE"


class JwtError(Exception):
    """JWT 디코드 실패. `reason` 으로 401 코드 분기 (`api/deps.py`)."""

    def __init__(self, reason: JwtErrorReason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token: str
    jti: str
    expires_at: datetime  # UTC aware


@dataclass(frozen=True, slots=True)
class DecodedToken:
    user_id: UUID
    jti: str
    token_type: TokenType
    expires_at: datetime  # UTC aware


def _now() -> datetime:
    return datetime.now(UTC)


def _require_secret() -> str:
    secret = get_settings().jwt_secret
    if not secret:
        raise RuntimeError("JWT_SECRET is not configured. Set it in .env or environment.")
    return secret


def _issue(user_id: UUID, token_type: TokenType, ttl: timedelta) -> IssuedToken:
    cfg = get_settings()
    secret = _require_secret()
    now = _now()
    exp = now + ttl
    jti = secrets.token_urlsafe(16)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "type": token_type,
        "jti": jti,
    }
    token = pyjwt.encode(payload, secret, algorithm=cfg.jwt_algorithm)
    return IssuedToken(token=token, jti=jti, expires_at=exp)


def issue_access_token(user_id: UUID) -> IssuedToken:
    cfg = get_settings()
    return _issue(user_id, "access", timedelta(minutes=cfg.jwt_access_token_ttl_minutes))


def issue_refresh_token(user_id: UUID) -> IssuedToken:
    cfg = get_settings()
    return _issue(user_id, "refresh", timedelta(days=cfg.jwt_refresh_token_ttl_days))


def decode_token(token: str, expected_type: TokenType) -> DecodedToken:
    """JWT 디코드 + 서명/만료 검증 + `type` 검증.

    Raises:
        JwtError(EXPIRED)    — `exp` 지남.
        JwtError(INVALID)    — 서명 불일치 / 파싱 실패 / 필수 클레임 누락.
        JwtError(WRONG_TYPE) — `type` 이 `expected_type` 과 다름.
    """
    cfg = get_settings()
    secret = _require_secret()
    try:
        payload: dict[str, Any] = pyjwt.decode(token, secret, algorithms=[cfg.jwt_algorithm])
    except pyjwt.ExpiredSignatureError as e:
        raise JwtError(JwtErrorReason.EXPIRED, str(e)) from e
    except pyjwt.PyJWTError as e:
        raise JwtError(JwtErrorReason.INVALID, str(e)) from e

    token_type = payload.get("type")
    if token_type != expected_type:
        raise JwtError(
            JwtErrorReason.WRONG_TYPE,
            f"expected {expected_type}, got {token_type}",
        )
    sub = payload.get("sub")
    jti = payload.get("jti")
    exp = payload.get("exp")
    if not sub or not jti or not isinstance(exp, int):
        raise JwtError(JwtErrorReason.INVALID, "missing required claims")
    try:
        user_id = UUID(sub)
    except (TypeError, ValueError) as e:
        raise JwtError(JwtErrorReason.INVALID, "sub is not a uuid") from e
    return DecodedToken(
        user_id=user_id,
        jti=str(jti),
        token_type=token_type,
        expires_at=datetime.fromtimestamp(exp, tz=UTC),
    )
