"""Idempotency-Key 미들웨어 (ADR-0002 §2.3 / api-contract.md §1.7)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from reaction_backend.api.exception_handlers import register_exception_handlers
from reaction_backend.api.middleware.idempotency import IdempotencyMiddleware


def _build_app() -> tuple[FastAPI, dict[str, int]]:
    """idempotent 경로(`/reflection/batch`)에 200 라우트를 둔 최소 앱.

    반환된 dict 의 `n` 으로 내부 라우트 실제 실행 횟수를 추적한다.
    """
    app = FastAPI()
    register_exception_handlers(app)
    calls = {"n": 0}

    @app.post("/reflection/batch")
    async def _batch() -> dict[str, int]:
        calls["n"] += 1
        return {"call": calls["n"]}

    app.add_middleware(IdempotencyMiddleware)
    return app, calls


def test_missing_key_returns_400() -> None:
    app, calls = _build_app()
    resp = TestClient(app).post("/reflection/batch")
    assert resp.status_code == 400
    assert resp.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert calls["n"] == 0  # 라우트까지 도달하지 않음


def test_same_key_replays_cached_response() -> None:
    app, calls = _build_app()
    client = TestClient(app)

    first = client.post("/reflection/batch", headers={"Idempotency-Key": "k1"})
    assert first.status_code == 200
    assert first.json() == {"call": 1}

    second = client.post("/reflection/batch", headers={"Idempotency-Key": "k1"})
    assert second.status_code == 200
    assert second.json() == {"call": 1}  # 캐시된 응답 — 라우트 재실행 안 됨
    assert second.headers.get("idempotent-replay") == "true"
    assert calls["n"] == 1


def test_same_key_different_body_returns_409() -> None:
    app, _ = _build_app()
    client = TestClient(app)
    client.post("/reflection/batch", headers={"Idempotency-Key": "k2"}, json={"a": 1})
    resp = client.post("/reflection/batch", headers={"Idempotency-Key": "k2"}, json={"a": 2})
    assert resp.status_code == 409
    assert resp.json()["code"] == "IDEMPOTENCY_KEY_MISMATCH"


def test_different_keys_run_independently() -> None:
    app, calls = _build_app()
    client = TestClient(app)
    r1 = client.post("/reflection/batch", headers={"Idempotency-Key": "a"})
    r2 = client.post("/reflection/batch", headers={"Idempotency-Key": "b"})
    assert r1.json() == {"call": 1}
    assert r2.json() == {"call": 2}
    assert calls["n"] == 2


# ── 캐시 스코프 (교차 재생 방지) ──


def _build_multi_route_app() -> tuple[FastAPI, dict[str, int]]:
    """idempotent 경로 2개 — 경로 간 캐시 격리 확인용."""
    app = FastAPI()
    register_exception_handlers(app)
    calls = {"n": 0}

    @app.post("/reflection/batch")
    async def _batch() -> dict[str, str]:
        calls["n"] += 1
        return {"route": "batch"}

    @app.post("/plans/replan/{execution_id}/approve")
    async def _approve(execution_id: str) -> dict[str, str]:
        calls["n"] += 1
        return {"route": "approve", "executionId": execution_id}

    app.add_middleware(IdempotencyMiddleware)
    return app, calls


def test_same_key_different_caller_does_not_replay() -> None:
    """같은 Idempotency-Key 라도 호출자가 다르면 남의 응답이 재생되지 않는다.

    회귀(실제 있었던 결함): 캐시 키가 헤더 값 하나뿐이라 user 스코프가 없었다.
    `POST /plans/replan/{id}/approve` 는 **body 가 없어** 모든 호출의 body_hash 가 sha256(b"")
    로 같아 mismatch 409 로도 안 걸렸고, FE 키가 `replan-{Date.now()}`(밀리초)라 같은 ms
    승인만으로 충돌한다. 그러면 남의 200 응답(scheduledBlockId·남의 executionId)이 그대로
    재생되고 정작 자기 블록은 생성되지 않았다.
    """
    app, calls = _build_multi_route_app()
    client = TestClient(app)

    mine = client.post(
        "/plans/replan/exec_A/approve",
        headers={"Idempotency-Key": "replan-1700000000000", "Authorization": "Bearer token-A"},
    )
    theirs = client.post(
        "/plans/replan/exec_B/approve",
        headers={"Idempotency-Key": "replan-1700000000000", "Authorization": "Bearer token-B"},
    )

    assert mine.json() == {"route": "approve", "executionId": "exec_A"}
    # 남의 응답이 재생되면 executionId 가 exec_A 로 나온다.
    assert theirs.json() == {"route": "approve", "executionId": "exec_B"}
    assert theirs.headers.get("idempotent-replay") is None
    assert calls["n"] == 2, "두 번째 호출이 캐시로 단락돼 실제로 실행되지 않았다"


def test_same_key_unauthenticated_cannot_replay_authenticated_response() -> None:
    """캐시 히트는 라우트 인증(Depends)을 타지 않는다 — 토큰 없는 호출이 남의 응답을 못 받는다."""
    app, calls = _build_multi_route_app()
    client = TestClient(app)

    client.post(
        "/plans/replan/exec_A/approve",
        headers={"Idempotency-Key": "shared", "Authorization": "Bearer token-A"},
    )
    anon = client.post("/plans/replan/exec_A/approve", headers={"Idempotency-Key": "shared"})

    assert anon.headers.get("idempotent-replay") is None, "인증 없이 캐시된 응답을 받았다"
    assert calls["n"] == 2


def test_same_key_different_endpoint_does_not_replay() -> None:
    """경로가 다르면 별개 네임스페이스 — DB 설계의 UNIQUE(user_id, endpoint, key) 와 정렬."""
    app, calls = _build_multi_route_app()
    client = TestClient(app)
    auth = {"Authorization": "Bearer token-A"}

    batch = client.post("/reflection/batch", headers={"Idempotency-Key": "same", **auth})
    approve = client.post(
        "/plans/replan/exec_A/approve", headers={"Idempotency-Key": "same", **auth}
    )

    assert batch.json()["route"] == "batch"
    assert approve.json()["route"] == "approve", "다른 endpoint 의 응답이 재생됐다"
    assert calls["n"] == 2


def test_same_caller_same_key_still_replays() -> None:
    """스코프를 좁혔어도 **원래 보장**(같은 호출자·같은 키 재시도)은 그대로 유지된다."""
    app, calls = _build_multi_route_app()
    client = TestClient(app)
    headers = {"Idempotency-Key": "retry-1", "Authorization": "Bearer token-A"}

    first = client.post("/plans/replan/exec_A/approve", headers=headers)
    second = client.post("/plans/replan/exec_A/approve", headers=headers)

    assert first.json() == second.json()
    assert second.headers.get("idempotent-replay") == "true"
    assert calls["n"] == 1
