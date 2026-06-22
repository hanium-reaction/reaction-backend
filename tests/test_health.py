"""Health · CORS · placeholder 501 분기.

Issue #16 이후 placeholder 라우터들도 `Depends(get_current_user)` 가 적용된다.
`client` fixture 는 인증 override 적용 상태 → placeholder 응답이 401 가려지지 않음.
"""

from fastapi.testclient import TestClient


def test_health_returns_response(client: TestClient) -> None:
    """앱이 살아있으면 200. DB 가용성과 무관하게 응답 자체는 보장."""
    response = client.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    assert body["app"] == "reaction-backend"
    assert "server_time" in body
    assert "db" in body
    assert isinstance(body["db"]["ok"], bool)
    if body["db"]["ok"]:
        assert body["db"]["latency_ms"] is not None and body["db"]["latency_ms"] >= 0
    else:
        assert body["db"]["error"]


def test_cors_preflight_allows_frontend_origin(client: TestClient) -> None:
    response = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_placeholder_routes_return_501(client: TestClient) -> None:
    """미구현 도메인 라우터는 501. 인증된 사용자 기준."""
    # /today/agenda 는 #19-A, /settings 는 #23-A, /recovery/proposals/generate 는 #20-A,
    # /reviews/weekly 는 #21-A 에서 구현됨 — placeholder 목록에서 제외. /replan/* 은 #20-B 까지 501 유지.
    for path in (
        "/plans/generate",
        "/reflection/batch",
        "/replan/exec_00000000-0000-4000-8000-000000000000/approve",
        "/policy-snapshot/current",
    ):
        method = "get" if path == "/policy-snapshot/current" else "post"
        resp = getattr(client, method)(path, headers={"Idempotency-Key": f"placeholder-{path}"})
        assert resp.status_code == 501, (
            f"{method.upper()} {path} should be 501, got {resp.status_code}"
        )
