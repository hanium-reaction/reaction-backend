"""공통 FastAPI dependency (Issue #16).

- `get_current_user`: `Authorization: Bearer <jwt>` 검증 → User 반환. 401 분기:
    * 헤더 누락 / Bearer 형식 오류 / 서명 불일치 / type≠access / DB user 없음 → `AUTH_INVALID_TOKEN`
    * 토큰 만료(`exp` 지남)                                                    → `AUTH_TOKEN_EXPIRED`
- `CurrentUser`: `def handler(user: CurrentUser)` 형태로 사용하는 alias.

`message` 는 화면에 그대로 띄우는 문구다(api-contract §1) — 다른 모든 엔드포인트와 같은
해요체로, **다음에 뭘 하면 되는지**까지 말한다. 네 갈래(헤더 없음/형식 오류/검증 실패/
계정 없음)는 사용자에게 다 같은 상황이라 다음 걸음도 하나다: 다시 로그인. 갈래별로
다르게 쓰는 건 분기용 정보를 문구에 흘리는 것이라, 구분이 필요한 쪽(FE·로그)은 `code`
로 한다. 'Bearer'·'토큰' 같은 내부 표기도 문구에서 뺐다 — 사용자가 고칠 수 있는 말이
아니다. (재검증 P3 — 여기만 합쇼체였다: "인증 헤더가 없습니다.")
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
            "로그인이 필요한 화면이에요. 다시 로그인해 주세요.",
            http_status=_UNAUTHORIZED,
        )

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise ApiError(
            ErrorCode.AUTH_INVALID_TOKEN,
            "로그인 정보를 읽지 못했어요. 다시 로그인해 주세요.",
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
            "로그인 정보가 더는 유효하지 않아요. 다시 로그인해 주세요.",
            http_status=_UNAUTHORIZED,
        ) from e

    user = await repo.get_by_id(decoded.user_id)
    if user is None:
        raise ApiError(
            ErrorCode.AUTH_INVALID_TOKEN,
            "계정 정보를 찾지 못했어요. 다시 로그인해 주세요.",
            http_status=_UNAUTHORIZED,
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
