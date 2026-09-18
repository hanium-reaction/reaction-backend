"""전역 예외 핸들러 — 모든 에러가 `ErrorResponse` 로 직렬화되는지 (ADR-0002 §2.2)."""

import re

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, field_validator

from reaction_backend.main import create_app
from reaction_backend.schemas.errors import ApiError, ErrorCode

_ERROR_KEYS = {"code", "message", "field", "server_time"}


def test_not_found_returns_error_response(client: TestClient) -> None:
    resp = client.get("/this-route-does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert set(body) == _ERROR_KEYS
    assert body["code"] == "COMMON_NOT_FOUND"
    assert body["server_time"].endswith("+09:00")


def test_not_implemented_error_is_error_response() -> None:
    """501 HTTPException 도 ErrorResponse(COMMON_NOT_IMPLEMENTED) 로 직렬화.

    도메인 placeholder 라우트(예: /recovery/...)에 결합하지 않고 전용 테스트 라우트로 검증한다 —
    각 도메인이 구현되어 501 이 사라져도 본 테스트가 깨지지 않도록(다른 PR 과의 결합 제거).
    """
    client = TestClient(_app_with_test_routes())
    resp = client.get("/__test__/not-implemented")
    assert resp.status_code == 501
    body = resp.json()
    assert set(body) == _ERROR_KEYS
    assert body["code"] == "COMMON_NOT_IMPLEMENTED"


def _app_with_test_routes() -> FastAPI:
    """검증·ApiError·미처리 예외를 일으키는 테스트 전용 라우트를 단 앱."""
    app = create_app()

    class _Body(BaseModel):
        count: int

    @app.post("/__test__/validate")
    async def _validate(body: _Body) -> dict[str, int]:
        return {"count": body.count}

    class _TitleBody(BaseModel):
        title: str = Field(min_length=1, max_length=200)
        tags: list[str] = Field(default_factory=list, max_length=3)

        @field_validator("title")
        @classmethod
        def _no_tilde(cls, v: str) -> str:
            if v.startswith("~"):
                raise ValueError("제목은 물결표로 시작할 수 없어요.")
            return v

    @app.post("/__test__/title")
    async def _title(body: _TitleBody) -> dict[str, str]:
        return {"title": body.title}

    @app.get("/__test__/api-error")
    async def _api_error() -> None:
        raise ApiError(
            ErrorCode.COMMON_NOT_FOUND,
            "데모 리소스를 찾지 못했어요.",
            http_status=404,
            field="demoId",
        )

    @app.get("/__test__/boom")
    async def _boom() -> None:
        raise RuntimeError("unexpected failure")

    @app.get("/__test__/not-implemented")
    async def _not_implemented() -> None:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="placeholder")

    return app


def test_validation_error_returns_422_error_response() -> None:
    client = TestClient(_app_with_test_routes())
    resp = client.post("/__test__/validate", json={"count": "not-an-int"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "COMMON_VALIDATION_ERROR"
    assert body["field"] == "count"


def test_api_error_uses_code_status_and_field() -> None:
    client = TestClient(_app_with_test_routes())
    resp = client.get("/__test__/api-error")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "COMMON_NOT_FOUND"
    assert body["message"] == "데모 리소스를 찾지 못했어요."
    assert body["field"] == "demoId"


def test_unhandled_error_returns_500_error_response() -> None:
    client = TestClient(_app_with_test_routes(), raise_server_exceptions=False)
    resp = client.get("/__test__/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert set(body) == _ERROR_KEYS
    assert body["code"] == "COMMON_INTERNAL_ERROR"


def test_unhandled_error_500_keeps_cors_and_request_id_headers() -> None:
    """처리 안 된 예외의 500 에도 CORS·`x-request-id` 가 붙어야 한다 (auth-4 / abuse-7).

    전역 `Exception` 핸들러는 Starlette 가 CORS **바깥**에서 돌려, 예전엔 500 에 ACAO 가
    빠졌다 — 크로스오리진으로 부르는 네이티브 앱은 그 응답을 네트워크 오류로 보고, 목표 화면이
    저장 안 된 가짜 목표를 끼워 넣었다. 422(ApiError 경로)에는 원래부터 붙어 있었다.
    """
    client = TestClient(_app_with_test_routes(), raise_server_exceptions=False)
    origin = "http://localhost:5173"  # 기본 cors_allow_origins
    resp = client.get("/__test__/boom", headers={"Origin": origin})

    assert resp.status_code == 500
    body = resp.json()
    assert set(body) == _ERROR_KEYS
    assert body["code"] == "COMMON_INTERNAL_ERROR"
    assert "unexpected failure" not in resp.text  # 예외 문구는 로그에만
    assert resp.headers.get("access-control-allow-origin") == origin
    assert resp.headers.get("x-request-id")


def test_api_error_still_uses_its_own_status_with_cors() -> None:
    """미들웨어가 ApiError 경로를 가로채지 않는다 — 제 상태·코드 그대로 + CORS."""
    client = TestClient(_app_with_test_routes(), raise_server_exceptions=False)
    origin = "http://localhost:5173"
    resp = client.get("/__test__/api-error", headers={"Origin": origin})

    assert resp.status_code == 404
    assert resp.json()["code"] == "COMMON_NOT_FOUND"
    assert resp.headers.get("access-control-allow-origin") == origin


# ── 사용자에게 보이는 검증 문구는 한국어 (abuse-8 / auth-5) ──
#
# FE 는 422 의 message 를 화면에 그대로 띄운다. 예전엔 pydantic 기본 영어 문구가 나갔다.

_HANGUL = re.compile(r"[가-힣]")


@pytest.mark.parametrize(
    ("path", "payload", "field", "expected"),
    [
        ("/__test__/validate", {"count": "not-an-int"}, "count", "형식이 올바르지 않아요"),
        ("/__test__/validate", {}, "count", "꼭 필요한 항목이 빠졌어요"),
        ("/__test__/title", {"title": ""}, "title", "내용을 입력해 주세요"),
        ("/__test__/title", {"title": "가" * 201}, "title", "200자까지 입력할 수 있어요"),
        ("/__test__/title", {"title": "x", "tags": ["a"] * 4}, "tags", "3개까지"),
    ],
)
def test_validation_messages_are_korean(
    path: str, payload: dict[str, object], field: str, expected: str
) -> None:
    client = TestClient(_app_with_test_routes())
    resp = client.post(path, json=payload)

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "COMMON_VALIDATION_ERROR"  # envelope·코드 그대로
    assert body["field"] == field
    assert expected in body["message"]
    for english in ("should", "Field required", "Input", "String"):
        assert english not in body["message"]


def test_title_of_exactly_max_length_is_accepted() -> None:
    client = TestClient(_app_with_test_routes())
    assert client.post("/__test__/title", json={"title": "가" * 200}).status_code == 200


def test_custom_korean_validator_message_passes_through_without_prefix() -> None:
    """스키마가 직접 쓴 한국어 문구는 그대로 — pydantic 의 'Value error, ' 접두어만 뗀다."""
    client = TestClient(_app_with_test_routes())
    resp = client.post("/__test__/title", json={"title": "~제목"})

    assert resp.status_code == 422
    assert resp.json()["message"] == "제목은 물결표로 시작할 수 없어요."


def test_malformed_json_body_message_is_korean() -> None:
    client = TestClient(_app_with_test_routes())
    resp = client.post(
        "/__test__/validate", content=b"{not json", headers={"content-type": "application/json"}
    )

    assert resp.status_code == 422
    assert _HANGUL.search(resp.json()["message"])
    assert "JSON" not in resp.json()["message"]


def test_starlette_404_and_405_messages_are_korean(client: TestClient) -> None:
    not_found = client.get("/this-route-does-not-exist").json()
    assert not_found["code"] == "COMMON_NOT_FOUND"
    assert _HANGUL.search(not_found["message"])
    assert "Not Found" not in not_found["message"]

    wrong_method = client.put("/auth/me")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["code"] == "COMMON_METHOD_NOT_ALLOWED"
    assert "Method Not Allowed" not in wrong_method.json()["message"]
