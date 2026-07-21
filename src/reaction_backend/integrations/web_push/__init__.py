"""Web Push 발송 (pywebpush + VAPID) — Issue #16/#20.

정책(예산·quiet hours·dedup)은 여기 없다 — `safety/push_gate.py` 가 유일한 enforce
지점이고, 이 패키지는 **전송만** 담당한다. 게이트를 거치지 않은 직접 호출 금지.
"""

from reaction_backend.integrations.web_push.sender import (
    SendOutcome,
    WebPushSender,
    get_web_push_sender,
)

__all__ = ["SendOutcome", "WebPushSender", "get_web_push_sender"]
