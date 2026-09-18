"""처리되지 않은 예외 → 500 `ErrorResponse` — **CORS·trace_id 미들웨어 안쪽에서** 만든다.

## 왜 전역 예외 핸들러만으로는 부족한가

`app.add_exception_handler(Exception, ...)` 로 등록한 핸들러는 Starlette 가 맨 바깥의
`ServerErrorMiddleware` 에서 실행한다. 그 위치는 `CORSMiddleware`·`CorrelationMiddleware`
**바깥**이라, 500 응답에는 `access-control-allow-origin` 도 `x-request-id` 도 붙지 않았다.

- 네이티브 앱(capacitor://localhost)은 백엔드를 크로스오리진으로 부른다. CORS 헤더가 없는
  응답은 WebView 가 막아 버려서, 앱은 서버 오류를 **네트워크 오류**로 본다 — 목표 화면은
  그걸 "서버가 꺼졌다"로 해석해 저장되지 않은 가짜 목표를 목록에 끼워 넣었다.
- `x-request-id` 가 없어 사용자가 문의해도 로그에서 그 요청을 찾을 손잡이가 없었다.

그래서 예외를 이 미들웨어가 먼저 받아, 같은 envelope 의 500 을 **여기서** 보낸다. 그러면
응답이 바깥의 Correlation·CORS 를 거쳐 나가면서 두 헤더를 받는다.

## 응답이 이미 시작됐으면 다시 던진다

헤더를 보낸 뒤에 난 예외(스트리밍 도중·백그라운드 태스크)는 두 번째 응답을 보낼 수 없다.
그때는 손대지 않고 올려 보내 기존 `ServerErrorMiddleware` 가 로그를 남기게 둔다.
전역 `Exception` 핸들러(`exception_handlers.py`)는 그 마지막 안전망으로 남겨 둔다.
"""

from __future__ import annotations

import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from reaction_backend.api.exception_handlers import internal_error_response
from reaction_backend.observability.correlation import get_trace_id

_log = logging.getLogger(__name__)


class UnhandledErrorMiddleware:
    """라우트에서 새어 나온 예외를 `COMMON_INTERNAL_ERROR` 500 으로 바꾼다."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_tracking(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_tracking)
        except Exception:
            if response_started:
                raise
            # 스택 트레이스는 로그에만 — 응답 본문에는 고정 문구만 싣는다. trace_id 는 바깥
            # CorrelationMiddleware 가 심은 값이라 응답의 `x-request-id` 와 같다(문의 → 로그).
            _log.exception(
                "unhandled error: %s %s trace_id=%s",
                scope.get("method", "?"),
                scope.get("path", "?"),
                get_trace_id(),
            )
            await internal_error_response()(scope, receive, send)
