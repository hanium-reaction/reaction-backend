"""WebPushSender — 전송 결과 분류 + timeout 계약 (#20 리뷰).

pywebpush 2.3.0 의 모듈 레벨 `webpush()` 는 기본 `timeout=None` 을 내부 `send()` 에
**명시 전달**해 requests 가 무한 대기한다 — endpoint 는 사용자 제공 URL 이라 블랙홀이
올 수 있고, 스레드가 물리면 max_instances=1 인 알림 cron 의 후속 폴이 전부 skip 되는
정지가 온다. 그래서 timeout 전달을 계약으로 고정한다.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pywebpush import WebPushException

from reaction_backend.integrations.web_push import sender as sender_module
from reaction_backend.integrations.web_push.sender import (
    _SEND_TIMEOUT_SECONDS,
    WebPushSender,
)

_SUBSCRIPTION = {"endpoint": "https://push.example.com/x", "keys": {"p256dh": "k", "auth": "a"}}
_PAYLOAD = {"class": "evening_reflection", "title": "t", "body": "b"}


def _sender() -> WebPushSender:
    return WebPushSender(private_key="priv", subject="mailto:dev@reaction.local")


async def test_unconfigured_returns_without_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(sender_module, "webpush", lambda **kw: calls.append(kw))

    outcome = await WebPushSender(private_key="", subject="").send(_SUBSCRIPTION, _PAYLOAD)

    assert outcome == "unconfigured"
    assert calls == []  # 키 없이 전송을 시도하지 않는다


async def test_ok_passes_timeout_and_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """timeout 이 반드시 전달된다 — 빠지면 requests 무한 대기(모듈 docstring)."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sender_module, "webpush", lambda **kw: calls.append(kw))

    outcome = await _sender().send(_SUBSCRIPTION, _PAYLOAD)

    assert outcome == "ok"
    (kw,) = calls
    assert kw["timeout"] == _SEND_TIMEOUT_SECONDS
    assert kw["subscription_info"] == _SUBSCRIPTION
    assert json.loads(kw["data"]) == _PAYLOAD  # payload 가 JSON 그대로 실린다
    assert kw["vapid_private_key"] == "priv"


@pytest.mark.parametrize("status", [404, 410])
async def test_gone_statuses_classified(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    def _raise(**kw: Any) -> None:
        raise WebPushException("gone", response=SimpleNamespace(status_code=status))

    monkeypatch.setattr(sender_module, "webpush", _raise)

    assert await _sender().send(_SUBSCRIPTION, _PAYLOAD) == "gone"


async def test_server_error_is_transient_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(**kw: Any) -> None:
        raise WebPushException("boom", response=SimpleNamespace(status_code=500))

    monkeypatch.setattr(sender_module, "webpush", _raise)

    assert await _sender().send(_SUBSCRIPTION, _PAYLOAD) == "error"


async def test_hard_timeout_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """스레드가 복귀하지 않아도 코루틴은 상한 안에 error 로 돌아온다 — cron 정지 방지."""
    import time as time_module

    monkeypatch.setattr(sender_module, "_SEND_HARD_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(sender_module, "webpush", lambda **kw: time_module.sleep(1))

    assert await _sender().send(_SUBSCRIPTION, _PAYLOAD) == "error"
