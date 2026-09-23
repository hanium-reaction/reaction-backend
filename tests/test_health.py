"""Health · CORS · placeholder 501 분기.

Issue #16 이후 placeholder 라우터들도 `Depends(get_current_user)` 가 적용된다.
`client` fixture 는 인증 override 적용 상태 → placeholder 응답이 401 가려지지 않음.
"""

import pytest
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


def test_reflection_batch_is_implemented(client: TestClient) -> None:
    """`POST /reflection/batch` 는 이제 실구현 — 더 이상 501 placeholder 가 아니다.

    과거 미구현 도메인 라우터들은 순차 구현됨: /today/agenda(#19-A) · /settings(#23-A)
    · /recovery/proposals/generate(#20-A) · /plans/generate(#32) · /reviews/weekly(#21-A)
    · /replan/*(#20-B) · /policy-snapshot/current(#83) · /reflection/batch(본 PR).
    남은 501 은 calendar connect/disconnect(P1, 의도적) 뿐 — test_calendar 에서 검증.
    """
    resp = client.post(
        "/reflection/batch",
        json={"items": []},
        headers={"Idempotency-Key": "placeholder-batch"},
    )
    assert resp.status_code == 200, (
        f"POST /reflection/batch should be implemented, got {resp.status_code}"
    )
    assert resp.json()["processedCount"] == 0


def test_health_does_not_leak_db_error_details(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """공개 /health 가 DB 예외 원문(내부 주소·DB 사용자명)을 싣지 않는다 (critic-11)."""
    from reaction_backend.api.routes import health
    from reaction_backend.config import get_settings

    def _boom() -> None:
        raise ConnectionRefusedError("Connect call failed ('10.0.0.5', 5432) role reaction_db")

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@10.0.0.5:5432/db")
    get_settings.cache_clear()
    monkeypatch.setattr(health, "get_engine", _boom)

    body = client.get("/health").json()

    assert body["status"] == "degraded"
    assert body["db"]["ok"] is False
    assert body["db"]["error"] == "db_unavailable"
    assert "10.0.0.5" not in str(body)
    assert "Connect call" not in str(body)
    assert "reaction_db" not in str(body)
