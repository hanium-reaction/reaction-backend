from fastapi.testclient import TestClient

from reaction_backend.main import app

client = TestClient(app)


def test_health_returns_response():
    """앱이 살아있으면 200. DB 가용성과 무관하게 응답 자체는 보장."""
    response = client.get("/health")
    assert response.status_code == 200

    body = response.json()
    # DB 연결 여부에 따라 ok 또는 degraded
    assert body["status"] in {"ok", "degraded"}
    assert body["app"] == "reaction-backend"
    assert "server_time" in body
    assert "db" in body
    assert isinstance(body["db"]["ok"], bool)
    # DB OK이면 latency_ms 양수
    if body["db"]["ok"]:
        assert body["db"]["latency_ms"] is not None and body["db"]["latency_ms"] >= 0
    else:
        # 실패 시 error 메시지가 있어야 디버깅 가능
        assert body["db"]["error"]


def test_cors_preflight_allows_frontend_origin():
    response = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_placeholder_routes_return_501():
    """16 도메인 라우터 중 health 외에는 모두 501."""
    for path in (
        "/auth/google",
        "/onboarding/status",
        "/interview/sessions",
        "/time-policies",
        "/goals",
        "/habits",
        "/plans/generate",
        "/calendar/connect",
        "/today/agenda",
        "/reflection/batch",
        "/recovery/proposals/generate",
        "/reviews/weekly",
        "/policy-snapshot/current",
        "/notifications/settings",
        "/settings",
    ):
        method = (
            "get"
            if path
            in {
                "/onboarding/status",
                "/time-policies",
                "/goals",
                "/habits",
                "/today/agenda",
                "/reviews/weekly",
                "/policy-snapshot/current",
                "/notifications/settings",
                "/settings",
            }
            else "post"
        )
        resp = getattr(client, method)(path)
        assert resp.status_code == 501, (
            f"{method.upper()} {path} should be 501, got {resp.status_code}"
        )
