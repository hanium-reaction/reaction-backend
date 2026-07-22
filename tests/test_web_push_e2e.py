"""Web Push 실발송 E2E — 로컬 push 서비스로 암호화·서명·전송을 **끝까지** 태운다 (#20).

왜 필요한가: 다른 push 테스트는 전부 `webpush` 를 monkeypatch 해서 **실제 암호화·서명이
한 번도 실행되지 않는다**. 그 상태로는 다음 회귀를 아무도 못 잡는다:

- `provision-vapid.yml` 이 만드는 키 형식(raw private value 32B → b64url)을 pywebpush 가
  못 읽게 되는 순간 — 라이브 발송이 **전량 실패**하는데 단위 테스트는 전부 초록이다.
  (이 형식 호환은 워크플로 작성 시점엔 **검증된 적 없는 가정**이었다.)
- payload 가 평문으로 나가거나(프라이버시), 브라우저가 복호화할 수 없는 형태로 나가는 것.

그래서 여기서는 브라우저 역할(P-256 키쌍 + auth secret)과 push 서비스 역할(로컬 HTTP
서버)을 세우고, **우리 sender 가 보낸 것을 실제로 복호화해** 원문과 대조한다.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from uuid import uuid4

import http_ece
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from reaction_backend.db.models.notification_setting import NotificationSetting
from reaction_backend.integrations.web_push.sender import WebPushSender
from reaction_backend.safety.push_gate import send_push
from reaction_backend.schemas.common import KST
from tests.conftest import FakeNotificationSendRepo

NOW = datetime(2026, 7, 22, 21, 0, tzinfo=KST)

PAYLOAD: dict[str, Any] = {
    "class": "evening_reflection",
    "title": "오늘의 회고 시간이에요",
    "body": "돌아볼 카드가 2장 있어요.",
    "url": "/reflection",
}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _vapid_keys_like_provision_workflow() -> tuple[str, str]:
    """`.github/workflows/provision-vapid.yml` 과 **동일한 방식**으로 키 쌍 생성.

    워크플로가 바뀌면 이 함수도 같이 바꿔야 한다 — 그때 이 테스트가 실 호환을 다시 증명한다.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    private_b64 = _b64url(key.private_numbers().private_value.to_bytes(32, "big"))
    public_b64 = _b64url(
        key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )
    return private_b64, public_b64


class _Browser:
    """브라우저 구독 역할 — 구독 객체를 내주고, 받은 암호문을 복호화한다."""

    def __init__(self, endpoint: str) -> None:
        self._priv = ec.generate_private_key(ec.SECP256R1())
        self._auth = os.urandom(16)
        self.endpoint = endpoint

    def subscription(self) -> dict[str, Any]:
        p256dh = self._priv.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": _b64url(p256dh), "auth": _b64url(self._auth)},
        }

    def decrypt(self, body: bytes) -> dict[str, Any]:
        raw = http_ece.decrypt(body, private_key=self._priv, auth_secret=self._auth)
        return dict(json.loads(raw.decode("utf-8")))


class _Handler(BaseHTTPRequestHandler):
    """push 서비스 역할 — 요청을 붙잡아 두고 지정한 상태코드를 돌려준다."""

    received: list[dict[str, Any]] = []
    status_to_return = 201

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        type(self).received.append(
            {
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": self.rfile.read(length),
            }
        )
        self.send_response(type(self).status_to_return)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        return  # 테스트 출력 오염 방지


@pytest.fixture
def push_service() -> Any:
    _Handler.received = []
    _Handler.status_to_return = 201
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/push/abc123"
    server.shutdown()
    server.server_close()


def _setting(subscription: dict[str, Any]) -> NotificationSetting:
    s = NotificationSetting()
    s.id = uuid4()
    s.user_id = uuid4()
    s.push_subscription = subscription
    return s


async def test_provision_workflow_keys_are_usable_by_pywebpush(push_service: str) -> None:
    """워크플로가 만든 키로 **실제 VAPID 서명**이 되고, 그 public key 가 헤더에 실린다.

    회귀: 키 형식이 어긋나면 라이브 발송이 전량 실패하는데 monkeypatch 테스트는 전부 통과한다.
    """
    private_key, public_key = _vapid_keys_like_provision_workflow()
    browser = _Browser(push_service)

    outcome = await WebPushSender(
        private_key=private_key, subject="mailto:dev@reaction.local"
    ).send(browser.subscription(), PAYLOAD)

    assert outcome == "ok"
    assert len(_Handler.received) == 1
    auth = _Handler.received[0]["headers"].get("authorization", "")
    assert public_key in auth, f"VAPID 헤더에 우리 public key 가 없다: {auth[:60]}"


async def test_payload_is_encrypted_and_decryptable_by_the_browser(push_service: str) -> None:
    """전송 본문이 암호문이고, 브라우저 키로 복호화하면 **원문과 정확히 일치**한다.

    평문 전송(프라이버시)과 복호화 불가(사용자에게 알림이 안 뜸) 양쪽을 동시에 막는다.
    """
    private_key, _ = _vapid_keys_like_provision_workflow()
    browser = _Browser(push_service)

    await WebPushSender(private_key=private_key, subject="mailto:dev@reaction.local").send(
        browser.subscription(), PAYLOAD
    )

    req = _Handler.received[0]
    assert req["headers"].get("content-encoding") == "aes128gcm"
    assert b"title" not in req["body"], "payload 가 평문으로 나갔다"
    assert PAYLOAD["title"].encode() not in req["body"]

    assert browser.decrypt(req["body"]) == PAYLOAD  # 한글 포함 왕복


@pytest.mark.parametrize(
    ("status_code", "expected"), [(404, "gone"), (410, "gone"), (500, "error")]
)
async def test_response_status_classification_over_real_http(
    push_service: str, status_code: int, expected: str
) -> None:
    """실제 HTTP 응답 → outcome 분류 (monkeypatch 가 아닌 진짜 왕복)."""
    _Handler.status_to_return = status_code
    private_key, _ = _vapid_keys_like_provision_workflow()

    outcome = await WebPushSender(private_key=private_key, subject="mailto:dev@x.local").send(
        _Browser(push_service).subscription(), PAYLOAD
    )

    assert outcome == expected


async def test_gate_and_real_transport_together(push_service: str) -> None:
    """게이트(잠금 규칙) + 실전송이 함께 도는 통합 경로 — 차단은 전송 시도조차 없어야 한다."""
    private_key, _ = _vapid_keys_like_provision_workflow()
    sender = WebPushSender(private_key=private_key, subject="mailto:dev@x.local")
    browser = _Browser(push_service)
    setting = _setting(browser.subscription())
    send_repo = FakeNotificationSendRepo()

    sent = await send_push(
        setting=setting,
        notification_class="evening_reflection",
        payload=PAYLOAD,
        now=NOW,
        send_repo=send_repo,  # type: ignore[arg-type]
        sender=sender,
    )
    assert sent.sent is True
    assert len(_Handler.received) == 1
    assert browser.decrypt(_Handler.received[0]["body"]) == PAYLOAD

    deduped = await send_push(
        setting=setting,
        notification_class="evening_reflection",
        payload=PAYLOAD,
        now=NOW + timedelta(minutes=5),
        send_repo=send_repo,  # type: ignore[arg-type]
        sender=sender,
    )
    assert deduped.reason == "class_dedup"
    assert len(_Handler.received) == 1, "차단됐는데 전송을 시도했다"

    quiet = await send_push(
        setting=setting,
        notification_class="pre_card",
        payload=PAYLOAD,
        now=datetime(2026, 7, 22, 23, 30, tzinfo=KST),
        send_repo=send_repo,  # type: ignore[arg-type]
        sender=sender,
    )
    assert quiet.reason == "quiet_hours"
    assert len(_Handler.received) == 1


async def test_gone_clears_subscription_over_real_http(push_service: str) -> None:
    """410 을 실제로 받았을 때 죽은 구독이 정리된다 — 다음 폴부터 재시도 낭비 없음."""
    _Handler.status_to_return = 410
    private_key, _ = _vapid_keys_like_provision_workflow()
    setting = _setting(_Browser(push_service).subscription())

    result = await send_push(
        setting=setting,
        notification_class="pre_card",
        payload=PAYLOAD,
        now=NOW,
        send_repo=FakeNotificationSendRepo(),  # type: ignore[arg-type]
        sender=WebPushSender(private_key=private_key, subject="mailto:dev@x.local"),
    )

    assert result.reason == "send_gone"
    assert setting.push_subscription is None
