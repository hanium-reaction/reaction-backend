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
from collections.abc import Callable
from typing import Any, Literal

import requests
from pywebpush import WebPushException, webpush

from reaction_backend.config import get_settings
from reaction_backend.integrations.web_push.endpoint import is_allowed_push_endpoint

_log = logging.getLogger(__name__)

# ok=발송 성공 · gone=구독 소멸(404/410, 구독 정리 대상) · error=일시 오류 ·
# unconfigured=VAPID 키 미설정 (발송 불가 환경)
SendOutcome = Literal["ok", "gone", "error", "unconfigured"]

# RFC 8030 §5.3 Urgency. 기기가 절전(Doze) 중일 때 push 서비스가 깨워서라도 전달할지를 가른다.
PushUrgency = Literal["very-low", "low", "normal", "high"]

# RFC 8030 §5.2 TTL(초) 기본값 — push 서비스가 기기가 오프라인일 때 메시지를 붙들고 있는 시간.
# pywebpush 기본은 **0**("지금 못 전하면 버려라")이다. push 서비스는 201 로 받아 놓고 절전·
# 오프라인 기기 몫을 즉시 버리는데, 게이트는 201 을 보고 발송으로 기록해 주 3건 예산을 쓴다
# → 사용자는 한 건도 못 받았는데 서버는 3건 보냈다고 믿는 주가 생긴다. 호출자(게이트)가 클래스별
# 값을 넘기고, 이 기본값은 어떤 경로로도 0 이 나가지 않게 하는 안전값이다.
DEFAULT_PUSH_TTL_SECONDS = 60 * 60

_GONE_STATUSES = (404, 410)

# 푸시 서비스 응답 대기 상한(초, requests 로 전달). endpoint 는 사용자 제공 URL 이라
# 응답을 물고 있는 블랙홀이 올 수 있다 — pywebpush 의 webpush() 는 timeout=None 이
# 기본이라(2.3.0, requests.post 무한 대기) 명시하지 않으면 스레드가 무한 점유되고,
# max_instances=1 인 cron 의 후속 폴이 전부 skip 되는 정지가 온다.
_SEND_TIMEOUT_SECONDS = 10.0
# to_thread 자체가 복귀하지 않는 최악까지 대비한 코루틴 상한 (이중 안전장치).
_SEND_HARD_TIMEOUT_SECONDS = 15.0


class _NoRedirectSession(requests.Session):
    """리다이렉트를 따라가지 않는 세션 — push 서비스는 201 로 답한다.

    requests 는 POST 의 30x 도 기본으로 따라간다. 허용 목록을 통과한 호스트라도 응답이 내부
    주소로 리다이렉트하면 그대로 도달하므로 여기서 끊는다. 30x 는 pywebpush 가 `> 202` 로
    보고 WebPushException 을 던져 `error` 로 분류된다.
    """

    def request(self, method: str, url: str | bytes, *args: Any, **kwargs: Any) -> Any:
        kwargs["allow_redirects"] = False
        return super().request(method, url, *args, **kwargs)


class WebPushSender:
    """VAPID 키 한 쌍으로 Web Push 를 보낸다. 정책 검사는 하지 않는다(게이트 책임).

    `allow_endpoint` 는 발송 직전 endpoint 재검사 — 기본은 push 서비스 허용 목록
    (`endpoint.py`). 스키마 검증이 생기기 전에 저장된 구독도 여기서 걸러진다(→ `gone` 으로
    분류해 게이트가 구독을 지운다). 로컬 push 서버로 실발송을 태우는 테스트만 바꿔 끼운다.
    """

    def __init__(
        self,
        *,
        private_key: str,
        subject: str,
        allow_endpoint: Callable[[str], bool] = is_allowed_push_endpoint,
    ) -> None:
        self._private_key = private_key
        self._subject = subject
        self._allow_endpoint = allow_endpoint

    @property
    def is_configured(self) -> bool:
        return bool(self._private_key and self._subject)

    async def send(
        self,
        subscription: dict[str, Any],
        payload: dict[str, Any],
        *,
        ttl: int = DEFAULT_PUSH_TTL_SECONDS,
        urgency: PushUrgency = "normal",
    ) -> SendOutcome:
        """`{endpoint, keys:{p256dh, auth}}` 구독으로 payload(JSON) 1건 발송.

        `ttl`(초)·`urgency` 는 RFC 8030 헤더 — 값은 게이트가 알림 클래스별로 정한다.
        """
        if not self.is_configured:
            return "unconfigured"
        endpoint = subscription.get("endpoint")
        if not isinstance(endpoint, str) or not self._allow_endpoint(endpoint):
            # 허용 목록 밖(내부 주소 등) — 요청을 보내지 않는다. `gone` 이면 게이트가 구독을
            # 지워 다음 폴부터 다시 시도하지 않는다. endpoint 값은 로그에 남기지 않는다.
            _log.warning("web push endpoint not allowed → treated as gone")
            return "gone"
        try:
            # pywebpush 는 동기(requests) — 이벤트 루프를 막지 않게 스레드로 내린다.
            await asyncio.wait_for(
                asyncio.to_thread(
                    webpush,
                    subscription_info=subscription,
                    data=json.dumps(payload, ensure_ascii=False),
                    vapid_private_key=self._private_key,
                    vapid_claims={"sub": self._subject},
                    timeout=_SEND_TIMEOUT_SECONDS,
                    ttl=max(int(ttl), 1),
                    headers={"Urgency": urgency},
                    requests_session=_NoRedirectSession(),
                ),
                timeout=_SEND_HARD_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            _log.warning("web push send timed out (>%ss)", _SEND_HARD_TIMEOUT_SECONDS)
            return "error"
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
