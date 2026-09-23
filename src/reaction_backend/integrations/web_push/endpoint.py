"""Web Push 구독 endpoint 허용 목록 — 서버가 **사용자가 준 URL 로 요청을 보내는** 유일한 경로.

구독 endpoint 는 브라우저가 만들어 주지만, API 로 들어오는 값은 결국 사용자가 고른 문자열이다.
검사 없이 저장하면 알림 cron 이 5분마다 EC2 에서 그 URL 로 VAPID 서명이 붙은 POST 를 보낸다 —
`http://169.254.169.254/...`(인스턴스 메타데이터)·`http://127.0.0.1:<port>`(Caddy admin 등
내부 서비스)·리다이렉트로 내부를 가리키는 외부 URL 모두 그대로 도달했다(blind SSRF).

실제 브라우저가 내주는 endpoint 는 소수의 push 서비스 호스트뿐이라 허용 목록이 좁고 안정적이다:

| 브라우저 | 호스트 |
| --- | --- |
| Chrome·Edge(Chromium)·Samsung·Opera·Android WebView | `fcm.googleapis.com` |
| Firefox | `updates.push.services.mozilla.com` (`*.push.services.mozilla.com`) |
| Safari(macOS·iOS 16.4+ PWA) | `web.push.apple.com` (`*.push.apple.com`) |
| 구 Edge(Windows WNS) | `*.notify.windows.com` |

https 가 아니거나, 호스트가 IP 리터럴이거나, 목록 밖이면 거절한다. 새 브라우저가 다른 push
서비스를 쓰게 되면 여기 한 곳만 늘린다(스키마 검증·발송 직전 재검사가 같은 함수를 쓴다).
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

# 정확히 이 호스트
_EXACT_HOSTS = frozenset(
    {
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "web.push.apple.com",
    }
)
# 이 접미사로 끝나는 하위 도메인 (앞의 점 포함 — `evilpush.apple.com` 같은 우회를 막는다)
_HOST_SUFFIXES = (
    ".push.services.mozilla.com",
    ".push.apple.com",
    ".notify.windows.com",
)


def is_allowed_push_endpoint(url: str) -> bool:
    """알려진 push 서비스의 https endpoint 인가."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port  # 잘못된 포트 문자열이면 여기서 ValueError
    except ValueError:
        return False
    if parts.scheme != "https" or not host:
        return False
    if parts.username is not None or parts.password is not None:
        return False  # `https://fcm.googleapis.com@evil/...` 류 혼동 차단
    if port not in (None, 443):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False  # IP 리터럴 — push 서비스는 항상 호스트 이름이다
    host = host.rstrip(".").lower()
    return host in _EXACT_HOSTS or host.endswith(_HOST_SUFFIXES)
