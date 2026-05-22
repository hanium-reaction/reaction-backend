"""Idempotency-Key 미들웨어 (ADR-0002 §2.3 / api-contract.md §1.7).

아래 5개 endpoint 는 `Idempotency-Key` 헤더가 **필수**다:

- POST /reflection/batch
- POST /recovery/decisions
- POST /replan/{execution_id}/approve
- POST /calendar/events/approve-insert
- POST /reviews/habit-penalty/{habit_id}/accept

동작:
- 같은 key 재요청 → 캐시된 응답 그대로 반환 (24h)
- 같은 key + 다른 요청 body → 409 `IDEMPOTENCY_KEY_MISMATCH`
- key 누락 → 400 `IDEMPOTENCY_KEY_REQUIRED`

Issue #3 단계 저장소는 in-memory(`InMemoryIdempotencyStore`). 영속(DB `idempotency_keys`)
백엔드는 도메인 실구현 시 `IdempotencyStore` 를 구현해 교체한다.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Protocol

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from reaction_backend.schemas.common import ErrorResponse
from reaction_backend.schemas.errors import ErrorCode

# Idempotency-Key 가 필수인 경로 (api-contract.md §1.7). path 파라미터는 [^/]+ 로 매칭.
_IDEMPOTENT_ROUTES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/reflection/batch$"),
    re.compile(r"^/recovery/decisions$"),
    re.compile(r"^/replan/[^/]+/approve$"),
    re.compile(r"^/calendar/events/approve-insert$"),
    re.compile(r"^/reviews/habit-penalty/[^/]+/accept$"),
)

_TTL_SECONDS = 24 * 60 * 60  # 24h 보장


def _requires_idempotency(method: str, path: str) -> bool:
    return method == "POST" and any(p.match(path) for p in _IDEMPOTENT_ROUTES)


@dataclass(slots=True)
class StoredResponse:
    """캐시된 응답 스냅샷."""

    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes
    body_hash: str


class IdempotencyStore(Protocol):
    """Idempotency 저장소 인터페이스. 도메인 실구현 시 DB 백엔드로 교체한다."""

    def get(self, key: str) -> StoredResponse | None: ...

    def put(self, key: str, value: StoredResponse) -> None: ...


class InMemoryIdempotencyStore:
    """프로세스 메모리 + TTL 저장소 (Issue #3 mock 한정).

    다중 워커·재기동에 취약하므로 도메인 실구현 시 DB 백엔드로 교체한다.
    """

    def __init__(self, ttl_seconds: int = _TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, tuple[StoredResponse, float]] = {}

    def get(self, key: str) -> StoredResponse | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() >= expires_at:
            del self._data[key]
            return None
        return value

    def put(self, key: str, value: StoredResponse) -> None:
        self._data[key] = (value, time.monotonic() + self._ttl)


def _header(scope: Scope, name: str) -> str | None:
    target = name.lower().encode("latin-1")
    for raw_key, raw_value in scope.get("headers", []):
        if raw_key.lower() == target:
            return bytes(raw_value).decode("latin-1")
    return None


async def _drain_body(receive: Receive) -> bytes:
    """요청 body 를 끝까지 읽어 버퍼링한다."""
    body = b""
    while True:
        message = await receive()
        if message["type"] == "http.request":
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        elif message["type"] == "http.disconnect":
            break
    return body


def _replay_receive(body: bytes) -> Receive:
    """버퍼링한 body 를 내부 앱에 한 번 다시 흘려보내는 receive 콜러블."""
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


async def _send_error(send: Send, status: int, code: ErrorCode, message: str) -> None:
    payload = ErrorResponse(code=code.value, message=message).model_dump(mode="json")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _send_stored(send: Send, stored: StoredResponse) -> None:
    headers = [*stored.headers, (b"idempotent-replay", b"true")]
    await send({"type": "http.response.start", "status": stored.status, "headers": headers})
    await send({"type": "http.response.body", "body": stored.body})


class _ResponseCapture:
    """내부 앱의 응답을 클라이언트로 흘려보내면서 동시에 캡처한다."""

    def __init__(self, send: Send) -> None:
        self._send = send
        self.status = 500
        self.headers: list[tuple[bytes, bytes]] = []
        self.body = b""

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = [(bytes(k), bytes(v)) for k, v in message.get("headers", [])]
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")
        await self._send(message)


class IdempotencyMiddleware:
    """Idempotency-Key 헤더를 강제·캐싱하는 ASGI 미들웨어."""

    def __init__(self, app: ASGIApp, store: IdempotencyStore | None = None) -> None:
        self.app = app
        self.store: IdempotencyStore = store if store is not None else InMemoryIdempotencyStore()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _requires_idempotency(scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return

        body = await _drain_body(receive)
        key = _header(scope, "idempotency-key")

        if not key:
            await _send_error(
                send,
                400,
                ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
                "이 작업에는 Idempotency-Key 헤더가 필요해요.",
            )
            return

        body_hash = hashlib.sha256(body).hexdigest()
        cached = self.store.get(key)
        if cached is not None:
            if cached.body_hash != body_hash:
                await _send_error(
                    send,
                    409,
                    ErrorCode.IDEMPOTENCY_KEY_MISMATCH,
                    "같은 Idempotency-Key 로 다른 요청이 들어왔어요.",
                )
                return
            await _send_stored(send, cached)
            return

        capture = _ResponseCapture(send)
        await self.app(scope, _replay_receive(body), capture.send)

        # 성공(2xx) 응답만 캐시 — 실패 응답은 재시도 가능하게 둔다.
        if 200 <= capture.status < 300:
            self.store.put(
                key,
                StoredResponse(
                    status=capture.status,
                    headers=capture.headers,
                    body=capture.body,
                    body_hash=body_hash,
                ),
            )
