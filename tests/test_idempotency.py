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
