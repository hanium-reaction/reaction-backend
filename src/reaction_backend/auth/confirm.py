"""2단계 확인 토큰 — 위험 작업(즉시 익명화 등) 재확인용 (Issue #23-B).

HMAC-SHA256(`JWT_SECRET`) 서명. step1 에서 발급 → step2 요청 본문으로 echo → 검증.
access/refresh JWT(`auth/jwt.py`)와 분리 — 동결 시그니처/revoke store 영향 없음.

포맷: `<base64url(payload json)>.<base64url(hmac)>`, payload = {sub, purpose, exp}.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from reaction_backend.config import get_settings

_DEFAULT_TTL = timedelta(minutes=5)


def _secret() -> bytes:
    secret = get_settings().jwt_secret
    if not secret:
        raise RuntimeError("JWT_SECRET is not configured.")
    return secret.encode("utf-8")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * ((-len(value)) % 4))


def _sign(body: str) -> str:
    digest = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def issue_confirmation_token(
    user_id: UUID,
    purpose: str,
    *,
    ttl: timedelta = _DEFAULT_TTL,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """확인 토큰 발급 → (token, 만료시각). 만료는 UTC aware."""
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + ttl
    payload = {"sub": str(user_id), "purpose": purpose, "exp": int(expires_at.timestamp())}
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_sign(body)}", expires_at


def verify_confirmation_token(
    token: str,
    user_id: UUID,
    purpose: str,
    *,
    now: datetime | None = None,
) -> bool:
    """서명·사용자·용도·만료 검증. 하나라도 어긋나면 False (상수시간 비교)."""
    parts = token.split(".")
    if len(parts) != 2:
        return False
    body, signature = parts
    if not hmac.compare_digest(signature, _sign(body)):
        return False
    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, TypeError):
        return False
    exp = payload.get("exp")
    if not isinstance(exp, int):
        return False
    current = now or datetime.now(UTC)
    return (
        payload.get("sub") == str(user_id)
        and payload.get("purpose") == purpose
        and exp >= int(current.timestamp())
    )
