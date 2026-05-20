from fastapi.testclient import TestClient

from reaction_backend.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "reaction-backend"
    assert "server_time" in body


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
