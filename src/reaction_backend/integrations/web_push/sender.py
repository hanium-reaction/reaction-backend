"""pywebpush 래퍼 — VAPID 서명 발송 + 결과 분류 (Issue #16/#20).

VAPID 미설정(키 없는 환경)이면 `unconfigured` 로 조용히 degrade 한다 — LLM 의
`GEMINI_API_KEY` 부재 패턴과 동일(앱·cron 은 정상 동작, 발송만 스킵). 라이브 키 주입은
`.github/workflows/provision-vapid.yml` (EC2 .env — deploy 가 rsync 로 덮지 않는 파일).

`gone`(404/410) 은 푸시 서비스가 구독을 폐기했다는 뜻 — 호출자(게이트)가 구독을
정리한다. 여기서는 분류만 한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal

from pywebpush import WebPushException, webpush

from reaction_backend.config import get_settings

_log = logging.getLogger(__name__)

# ok=발송 성공 · gone=구독 소멸(404/410, 구독 정리 대상) · error=일시 오류 ·
# unconfigured=VAPID 키 미설정 (발송 불가 환경)
SendOutcome = Literal["ok", "gone", "error", "unconfigured"]

_GONE_STATUSES = (404, 410)


class WebPushSender:
    """VAPID 키 한 쌍으로 Web Push 를 보낸다. 정책 검사는 하지 않는다(게이트 책임)."""

    def __init__(self, *, private_key: str, subject: str) -> None:
        self._private_key = private_key
        self._subject = subject

    @property
    def is_configured(self) -> bool:
        return bool(self._private_key and self._subject)

    async def send(self, subscription: dict[str, Any], payload: dict[str, Any]) -> SendOutcome:
        """`{endpoint, keys:{p256dh, auth}}` 구독으로 payload(JSON) 1건 발송."""
        if not self.is_configured:
            return "unconfigured"
        try:
            # pywebpush 는 동기(requests) — 이벤트 루프를 막지 않게 스레드로 내린다.
            await asyncio.to_thread(
                webpush,
                subscription_info=subscription,
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=self._private_key,
                vapid_claims={"sub": self._subject},
            )
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            if status in _GONE_STATUSES:
                return "gone"
            _log.warning("web push send failed (status=%s): %s", status, e)
            return "error"
        except Exception:  # noqa: BLE001 — 전송 실패가 cron 사용자 루프를 멈추면 안 된다
            _log.exception("web push send failed (transport)")
            return "error"
        return "ok"


def get_web_push_sender() -> WebPushSender:
    settings = get_settings()
    return WebPushSender(
        private_key=settings.vapid_private_key,
        subject=settings.vapid_subject,
    )
