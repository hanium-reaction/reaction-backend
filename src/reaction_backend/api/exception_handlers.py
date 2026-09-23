"""전역 예외 핸들러 — 모든 에러를 `ErrorResponse` 한 형태로 직렬화한다 (ADR-0002 §2.2).

- `ApiError`              → `code`/`message`/`field` 그대로, `http_status` 적용
- `RequestValidationError`→ 422 `COMMON_VALIDATION_ERROR`, 첫 위반 필드 표기 + **한국어 문구**
- `HTTPException`         → status code 보존하며 `ErrorResponse` 로 정규화 (영어 detail 은 한국어로)
- DB 문자열 길이 초과(SQLSTATE 22001) → 422 `COMMON_VALIDATION_ERROR` "너무 길어요" (나머지 DB 오류는 500)
- 그 외 `Exception`       → 500 `COMMON_INTERNAL_ERROR` (스택 트레이스 비노출).
  실제로는 `middleware/unhandled_error.py` 가 CORS 안쪽에서 먼저 받는다 — 여기 핸들러는
  Starlette 가 CORS **바깥**에서 실행해 500 에 CORS·`x-request-id` 가 빠지기 때문이다.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any, cast

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from reaction_backend.schemas.common import ErrorResponse
from reaction_backend.schemas.errors import ApiError, ErrorCode

_log = logging.getLogger(__name__)

# HTTPException status code → 공통 에러 코드 매핑
_STATUS_TO_CODE: dict[int, ErrorCode] = {
    404: ErrorCode.COMMON_NOT_FOUND,
    405: ErrorCode.COMMON_METHOD_NOT_ALLOWED,
    422: ErrorCode.COMMON_VALIDATION_ERROR,
    500: ErrorCode.COMMON_INTERNAL_ERROR,
    501: ErrorCode.COMMON_NOT_IMPLEMENTED,
}

# Pydantic 검증 에러 loc 의 위치 prefix (필드명에서 제거)
_LOC_PREFIXES = frozenset({"body", "query", "path", "header", "cookie"})

# ── 사용자에게 보이는 검증 문구 ──
# FE 는 422 의 `message` 를 화면에 그대로 띄운다. 예전엔 pydantic 기본 문구가 그대로 나가
# 'String should have at least 1 character'·'Field required' 같은 영어가 사용자에게 보였다.
# - 문구에 한글이 이미 있으면(스키마가 PydanticCustomError/ValueError 로 직접 쓴 한국어) 그대로.
# - 아니면 pydantic 에러 종류(type)별 한국어로 바꾼다. 원문은 `field` 로 위치만 남긴다.
_HANGUL = re.compile(r"[가-힣]")
_VALUE_ERROR_PREFIX = "Value error, "
_GENERIC_VALIDATION_MESSAGE = "입력한 내용을 한 번 더 확인해 주세요."
_FORMAT_MESSAGE = "형식이 올바르지 않아요. 한 번 더 확인해 주세요."
_BAD_REQUEST_MESSAGE = "요청을 읽지 못했어요. 잠시 후 다시 시도해 주세요."
_RANGE_MESSAGE = "허용된 범위를 벗어났어요. 한 번 더 확인해 주세요."
_CHOICE_MESSAGE = "고를 수 없는 값이에요. 목록에서 골라 주세요."
_TYPE_MESSAGES: dict[str, str] = {
    "missing": "꼭 필요한 항목이 빠졌어요.",
    "json_invalid": _BAD_REQUEST_MESSAGE,
    "json_type": _BAD_REQUEST_MESSAGE,
    "model_type": _BAD_REQUEST_MESSAGE,
    "model_attributes_type": _BAD_REQUEST_MESSAGE,
    "dict_type": _BAD_REQUEST_MESSAGE,
    "extra_forbidden": _BAD_REQUEST_MESSAGE,
    "string_pattern_mismatch": _FORMAT_MESSAGE,
    "greater_than": _RANGE_MESSAGE,
    "greater_than_equal": _RANGE_MESSAGE,
    "less_than": _RANGE_MESSAGE,
    "less_than_equal": _RANGE_MESSAGE,
    "literal_error": _CHOICE_MESSAGE,
    "enum": _CHOICE_MESSAGE,
}

# Starlette 기본 detail('Not Found' 등)이 그대로 나가지 않게 — 상태별 한국어.
_STATUS_MESSAGES: dict[int, str] = {
    404: "찾는 내용이 없어요.",
    405: "지원하지 않는 요청이에요.",
    501: "아직 준비 중인 기능이에요.",
}


def _error_json(
    http_status: int, error: ErrorResponse, *, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=http_status, content=error.model_dump(mode="json"), headers=headers
    )


async def _handle_api_error(request: Request, exc: Exception) -> Response:
    err = cast(ApiError, exc)
    return _error_json(
        err.http_status,
        ErrorResponse(code=err.code.value, message=err.message, field=err.field),
        headers=err.headers,
    )


def _validation_message(error: dict[str, Any]) -> str:
    """pydantic 에러 1건 → 사용자에게 보일 한국어 문구 (위 `_TYPE_MESSAGES` 주석)."""
    raw = str(error.get("msg") or "")
    if raw.startswith(_VALUE_ERROR_PREFIX):
        raw = raw[len(_VALUE_ERROR_PREFIX) :]
    if _HANGUL.search(raw):
        return raw
    kind = str(error.get("type") or "")
    ctx = error.get("ctx") or {}
    if kind == "string_too_short":
        minimum = ctx.get("min_length", 1)
        return "내용을 입력해 주세요." if minimum <= 1 else f"{minimum}자 이상 입력해 주세요."
    if kind == "string_too_long":
        maximum = ctx.get("max_length")
        if maximum is None:
            return "너무 길어요. 조금 줄여 주세요."
        return f"{maximum}자까지 입력할 수 있어요. 조금 줄여 주세요."
    if kind == "too_short":
        minimum = ctx.get("min_length", 1)
        return "하나 이상 골라 주세요." if minimum <= 1 else f"{minimum}개 이상 필요해요."
    if kind == "too_long":
        maximum = ctx.get("max_length")
        return f"{maximum}개까지 보낼 수 있어요." if maximum is not None else "너무 많아요."
    if kind in _TYPE_MESSAGES:
        return _TYPE_MESSAGES[kind]
    if kind.endswith(("_parsing", "_type")):
        return _FORMAT_MESSAGE
    return _GENERIC_VALIDATION_MESSAGE


def _field_from_loc(loc: Sequence[Any]) -> str | None:
    """pydantic `loc` → 사용자 요청의 필드 경로. 진짜 필드가 아니면 None.

    본문이 JSON 으로 **읽히지도 않으면**(`{not json`) pydantic 은 loc 에 필드 이름 대신
    깨진 **문자 위치**를 넣는다(`("body", 1)`). 그대로 이어 붙이면 `field: "1"` 이 나가,
    FE 는 있지도 않은 '1' 필드에 빨간 줄을 그리려다 아무 칸도 못 찾는다. 이름이 하나도 없는
    loc 은 가리킬 필드가 없다는 뜻이므로 비운다(문구는 종전대로 "요청을 읽지 못했어요…").
    배열 원소 오류(`("body", "daysOfWeek", 0)` → `daysOfWeek.0`)처럼 이름이 하나라도 있으면
    종전 그대로 경로를 싣는다.
    """
    parts = [p for p in loc if p not in _LOC_PREFIXES]
    if not any(isinstance(p, str) for p in parts):
        return None
    return ".".join(str(p) for p in parts)


async def _handle_validation_error(request: Request, exc: Exception) -> Response:
    err = cast(RequestValidationError, exc)
    details = err.errors()
    first = details[0] if details else None
    field: str | None = None
    message = _GENERIC_VALIDATION_MESSAGE
    if first is not None:
        field = _field_from_loc(first.get("loc", ()))
        message = _validation_message(first)
    return _error_json(
        422,
        ErrorResponse(code=ErrorCode.COMMON_VALIDATION_ERROR.value, message=message, field=field),
    )


async def _handle_http_exception(request: Request, exc: Exception) -> Response:
    err = cast(StarletteHTTPException, exc)
    code = _STATUS_TO_CODE.get(err.status_code, ErrorCode.COMMON_INTERNAL_ERROR)
    detail = err.detail if isinstance(err.detail, str) else ""
    if _HANGUL.search(detail):
        message = detail  # 라우트가 직접 쓴 한국어 문구
    else:
        # 'Not Found'·'Method Not Allowed' 같은 Starlette 기본 영어 문구를 그대로 보이지 않는다.
        message = _STATUS_MESSAGES.get(err.status_code, "요청을 처리하지 못했어요.")
    return _error_json(err.status_code, ErrorResponse(code=code.value, message=message))


def internal_error_response() -> JSONResponse:
    """500 `COMMON_INTERNAL_ERROR` — 스택 트레이스 없이 고정 문구만.

    `UnhandledErrorMiddleware`(CORS 안쪽)와 아래 최후 핸들러가 같은 응답을 쓰도록 한 곳에 둔다.
    """
    return _error_json(
        500,
        ErrorResponse(
            code=ErrorCode.COMMON_INTERNAL_ERROR.value,
            message="서버 내부 오류가 발생했어요. 잠시 후 다시 시도해 주세요.",
        ),
    )


# PostgreSQL `string_data_right_truncation` — 값이 VARCHAR(n) 보다 길다.
_SQLSTATE_STRING_TOO_LONG = "22001"


async def _handle_db_error(request: Request, exc: Exception) -> Response:
    """DB 오류 — 문자열 길이 초과만 입력 문제(422)로, 나머지는 500.

    스키마에 길이 상한이 빠진 입력(제목 등)이 컬럼 `String(n)` 을 넘으면 asyncpg 가
    StringDataRightTruncation 을 던져 500 이 됐다(auth-5). 상한은 스키마가 1차로 막고, 이건
    빠진 곳이 남아도 사용자가 "줄이면 된다"는 걸 알 수 있게 하는 안전망이다.
    """
    err = cast(DBAPIError, exc)
    if getattr(err.orig, "sqlstate", None) == _SQLSTATE_STRING_TOO_LONG:
        _log.warning("db string too long on %s %s", request.method, request.url.path)
        return _error_json(
            422,
            ErrorResponse(
                code=ErrorCode.COMMON_VALIDATION_ERROR.value,
                message="입력한 내용이 너무 길어요. 조금 줄여 주세요.",
            ),
        )
    _log.error("unhandled db error on %s %s", request.method, request.url.path, exc_info=exc)
    return internal_error_response()


async def _handle_unhandled_error(request: Request, exc: Exception) -> Response:
    # 최후 안전망 — 평소엔 `UnhandledErrorMiddleware` 가 먼저 받아 CORS 헤더가 붙은 500 을
    # 보낸다. 여기까지 오는 건 응답이 이미 시작된 뒤의 예외 등 그 미들웨어 밖의 경우뿐이다.
    return internal_error_response()


def register_exception_handlers(app: FastAPI) -> None:
    """전역 예외 핸들러를 앱에 등록한다. `create_app()` 에서 호출."""
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(DBAPIError, _handle_db_error)
    app.add_exception_handler(Exception, _handle_unhandled_error)
