"""Health · CORS · placeholder 501 분기.

Issue #16 이후 placeholder 라우터들도 `Depends(get_current_user)` 가 적용된다.
`client` fixture 는 인증 override 적용 상태 → placeholder 응답이 401 가려지지 않음.
"""

import pytest
from fastapi.testclient import TestClient

from reaction_backend.schemas.common import DbStatus


def _db_is(monkeypatch: pytest.MonkeyPatch, db: DbStatus) -> None:
    """DB 핑 결과를 고정한다 — 실행 환경의 실제 DB 상태에 따라 결과가 바뀌지 않게."""
    from reaction_backend.api.routes import health

    async def _fixed(_database_url: str) -> DbStatus:
        return db

    monkeypatch.setattr(health, "_check_db", _fixed)


def test_health_is_200_when_db_is_reachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _db_is(monkeypatch, DbStatus(ok=True, latency_ms=3))

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "reaction-backend"
    assert "server_time" in body
    assert body["db"] == {"ok": True, "latency_ms": 3, "error": None}


def test_health_is_503_when_db_is_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DB 장애는 서비스 장애다 — 상태 코드만 보는 업타임 감시가 잡을 수 있어야 한다.

    예전엔 degraded 도 200 이라 외부 감시·Docker HEALTHCHECK 가 장애를 정상으로 읽었다.
    본문은 200 일 때와 같은 모양이어야 한다 — 워크플로가 본문을 로그로 남긴다.
    """
    _db_is(monkeypatch, DbStatus(ok=False, error="db_unavailable"))

    response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["app"] == "reaction-backend"
    assert "server_time" in body
    assert body["db"] == {"ok": False, "latency_ms": None, "error": "db_unavailable"}


def test_health_is_503_when_database_url_is_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DB 설정이 빠진 배포도 '살아 있음'으로 보이면 안 된다."""
    from reaction_backend.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "")
    get_settings.cache_clear()

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["db"]["error"] == "DATABASE_URL not configured"


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

    response = client.get("/health")
    body = response.json()

    assert response.status_code == 503
    assert body["status"] == "degraded"
    assert body["db"]["ok"] is False
    assert body["db"]["error"] == "db_unavailable"
    assert "10.0.0.5" not in str(body)
    assert "Connect call" not in str(body)
    assert "reaction_db" not in str(body)
