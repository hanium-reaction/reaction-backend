"""공통 FastAPI dependency (Issue #16).

- `get_current_user`: `Authorization: Bearer <jwt>` 검증 → User 반환. 401 분기:
    * 헤더 누락 / Bearer 형식 오류 / 서명 불일치 / type≠access / DB user 없음 → `AUTH_INVALID_TOKEN`
    * 토큰 만료(`exp` 지남)                                                    → `AUTH_TOKEN_EXPIRED`
- `CurrentUser`: `def handler(user: CurrentUser)` 형태로 사용하는 alias.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, Header

from reaction_backend.auth.jwt import JwtError, JwtErrorReason, decode_token
from reaction_backend.db.models.user import User
from reaction_backend.repositories.user_repo import UserRepo, get_user_repo
from reaction_backend.schemas.errors import ApiError, ErrorCode

_UNAUTHORIZED = HTTPStatus.UNAUTHORIZED


async def get_current_user(
    repo: Annotated[UserRepo, Depends(get_user_repo)],
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    if authorization is None:
        raise ApiError(
            ErrorCode.AUTH_INVALID_TOKEN,
            "인증 헤더가 없습니다.",
            http_status=_UNAUTHORIZED,
        )

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise ApiError(
            ErrorCode.AUTH_INVALID_TOKEN,
            "Bearer 토큰 형식이 아닙니다.",
            http_status=_UNAUTHORIZED,
        )
    token = parts[1].strip()

    try:
        decoded = decode_token(token, expected_type="access")
    except JwtError as e:
        if e.reason is JwtErrorReason.EXPIRED:
            raise ApiError(
                ErrorCode.AUTH_TOKEN_EXPIRED,
                "세션이 만료됐어요. 다시 로그인해 주세요.",
                http_status=_UNAUTHORIZED,
            ) from e
        raise ApiError(
            ErrorCode.AUTH_INVALID_TOKEN,
            "인증 토큰이 유효하지 않습니다.",
            http_status=_UNAUTHORIZED,
        ) from e

    user = await repo.get_by_id(decoded.user_id)
    if user is None:
        raise ApiError(
            ErrorCode.AUTH_INVALID_TOKEN,
            "사용자를 찾을 수 없습니다.",
            http_status=_UNAUTHORIZED,
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
