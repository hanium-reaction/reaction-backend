"""전역 예외 핸들러 — 모든 에러를 `ErrorResponse` 한 형태로 직렬화한다 (ADR-0002 §2.2).

- `ApiError`              → `code`/`message`/`field` 그대로, `http_status` 적용
- `RequestValidationError`→ 422 `COMMON_VALIDATION_ERROR`, 첫 위반 필드 표기
- `HTTPException`         → status code 보존하며 `ErrorResponse` 로 정규화
- 그 외 `Exception`       → 500 `COMMON_INTERNAL_ERROR` (스택 트레이스 비노출)
"""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from reaction_backend.schemas.common import ErrorResponse
from reaction_backend.schemas.errors import ApiError, ErrorCode

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


def _error_json(http_status: int, error: ErrorResponse) -> JSONResponse:
    return JSONResponse(status_code=http_status, content=error.model_dump(mode="json"))


async def _handle_api_error(request: Request, exc: Exception) -> Response:
    err = cast(ApiError, exc)
    return _error_json(
        err.http_status,
        ErrorResponse(code=err.code.value, message=err.message, field=err.field),
    )


async def _handle_validation_error(request: Request, exc: Exception) -> Response:
    err = cast(RequestValidationError, exc)
    details = err.errors()
    first = details[0] if details else None
    field: str | None = None
    message = "요청 형식이 올바르지 않습니다."
    if first is not None:
        loc = [str(p) for p in first.get("loc", ()) if p not in _LOC_PREFIXES]
        field = ".".join(loc) or None
        message = first.get("msg", message)
    return _error_json(
        422,
        ErrorResponse(code=ErrorCode.COMMON_VALIDATION_ERROR.value, message=message, field=field),
    )


async def _handle_http_exception(request: Request, exc: Exception) -> Response:
    err = cast(StarletteHTTPException, exc)
    code = _STATUS_TO_CODE.get(err.status_code, ErrorCode.COMMON_INTERNAL_ERROR)
    message = err.detail if isinstance(err.detail, str) and err.detail else code.value
    return _error_json(err.status_code, ErrorResponse(code=code.value, message=message))


async def _handle_unhandled_error(request: Request, exc: Exception) -> Response:
    return _error_json(
        500,
        ErrorResponse(
            code=ErrorCode.COMMON_INTERNAL_ERROR.value,
            message="서버 내부 오류가 발생했어요. 잠시 후 다시 시도해 주세요.",
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """전역 예외 핸들러를 앱에 등록한다. `create_app()` 에서 호출."""
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unhandled_error)
