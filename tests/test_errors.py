"""전역 예외 핸들러 — 모든 에러가 `ErrorResponse` 로 직렬화되는지 (ADR-0002 §2.2)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

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


def test_placeholder_route_error_is_error_response(client: TestClient) -> None:
    # 아직 미구현인 placeholder 라우트 (recovery 는 #20 에서 구현 예정; planning 은 #32 구현 완료)
    resp = client.post("/recovery/proposals/generate")
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
