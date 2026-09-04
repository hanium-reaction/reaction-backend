"""Google OAuth 토큰 교환·갱신 — 캘린더 연결의 자격증명 절반 (#17 해제).

`google_oauth/verifier.py` 와 다른 일을 한다. 그쪽은 **로그인**(id_token 검증)이고
여기는 **위임 접근**(authorization code → access/refresh token)이다. 같은 Google
프로젝트의 client_id 를 쓰지만 흐름이 달라 모듈을 나눈다.

## 왜 google-api-python-client 를 안 쓰나

그 라이브러리는 동기이고 무겁다(discovery 문서 캐싱·httplib2). 우리가 필요한 건
토큰 엔드포인트 POST 하나와 freebusy POST 하나뿐이라, 레포에 이미 있는 `requests` 를
`web_fetch/fetcher.py`·`web_push/sender.py` 와 같은 방식으로 감싼다 —
`to_thread` + **이중 timeout**. 새 외부 의존성이 0 이다(AGENTS §8).

## 범위

**읽기 전용이다.** 스코프는 `calendar.freebusy` 하나 — 구간의 길이와 인접성만 있으면
스케줄러의 세 룰(전이 버퍼·부하 감쇠·자투리)이 전부 성립하고, 제목·장소는 필요 없다
(ADR-0009 D4). `calendar.readonly` 로 넓히면 남의 일정 제목이 우리 DB 근처로 오는데
그만한 값이 없다. `events.insert`(write-back)는 **P1 유지** — 이 모듈은 쓰기를 모른다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import requests

from reaction_backend.config import get_settings

logger = logging.getLogger(__name__)

#: 최소 권한 — freebusy 만. 넓히려면 ADR 을 먼저 고칠 것.
CALENDAR_SCOPE: Final = "https://www.googleapis.com/auth/calendar.freebusy"

_TOKEN_URL: Final = "https://oauth2.googleapis.com/token"
_REVOKE_URL: Final = "https://oauth2.googleapis.com/revoke"

# 사용자가 화면 앞에서 기다리는 왕복이라 짧게. 선례: web_fetch/fetcher.py
_CONNECT_TIMEOUT: Final = 3.0
_READ_TIMEOUT: Final = 5.0
#: to_thread 가 복귀하지 않는 최악까지 대비한 코루틴 상한.
_HARD_TIMEOUT: Final = 10.0

#: 만료 판정 여유 — 네트워크 왕복 중에 만료되는 경계를 피한다.
REFRESH_SKEW: Final = timedelta(seconds=60)


class OAuthError(RuntimeError):
    """토큰 교환·갱신 실패. 호출자가 401/404 로 변환한다."""

    def __init__(self, reason: str, *, retryable: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        #: True 면 일시적(네트워크·5xx) — 사용자에게 재연결을 요구하지 않는다.
        self.retryable = retryable


@dataclass(frozen=True)
class TokenBundle:
    """토큰 교환·갱신 결과.

    `refresh_token` 은 **갱신 응답에는 보통 없다** — Google 은 최초 동의 때만 준다.
    그래서 None 이 정상이고, 호출자는 기존 값을 유지해야 한다(덮어쓰면 연결이 끊긴다).
    """

    access_token: str
    expires_at: datetime
    refresh_token: str | None
    scopes: str


def _post(url: str, data: dict[str, str]) -> requests.Response:
    return requests.post(
        url,
        data=data,
        timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
    )


async def _post_async(url: str, data: dict[str, str]) -> requests.Response:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_post, url, data), timeout=_HARD_TIMEOUT)
    except TimeoutError as exc:
        raise OAuthError("timeout", retryable=True) from exc
    except requests.RequestException as exc:
        raise OAuthError("network", retryable=True) from exc


def _bundle_from(payload: dict[str, Any], *, fallback_scopes: str) -> TokenBundle:
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise OAuthError("no_access_token", retryable=False)
    # expires_in 이 없으면 보수적으로 짧게 잡는다 — 길게 잡아 만료된 토큰을 쓰는 것보다
    # 한 번 더 갱신하는 편이 안전하다.
    expires_in = payload.get("expires_in")
    seconds = int(expires_in) if isinstance(expires_in, int | float | str) else 300
    refresh = payload.get("refresh_token")
    scope = payload.get("scope")
    return TokenBundle(
        access_token=access,
        expires_at=datetime.now(UTC) + timedelta(seconds=seconds),
        refresh_token=refresh if isinstance(refresh, str) and refresh else None,
        scopes=scope if isinstance(scope, str) and scope else fallback_scopes,
    )


def _raise_for_error(response: requests.Response) -> dict[str, Any]:
    if response.status_code >= 500:
        raise OAuthError(f"google_5xx_{response.status_code}", retryable=True)
    try:
        payload: dict[str, Any] = response.json()
    except ValueError as exc:
        raise OAuthError("bad_json", retryable=False) from exc
    if response.status_code >= 400:
        # Google 은 실패 사유를 error 필드에 담는다(invalid_grant = 만료·철회된 코드/토큰).
        reason = str(payload.get("error", f"http_{response.status_code}"))
        raise OAuthError(reason, retryable=False)
    return payload


async def exchange_code(code: str, *, redirect_uri: str | None = None) -> TokenBundle:
    """authorization code → 토큰 (최초 연결).

    `access_type=offline` 로 동의를 받아야 refresh_token 이 온다 — 그건 **동의 URL 을
    만드는 클라이언트 책임**이다. 여기서 refresh_token 이 안 오면 다음 갱신이 불가능하니
    연결 자체를 실패로 본다(반쯤 된 연결을 저장하면 하루 뒤에 조용히 죽는다).
    """
    cfg = get_settings()
    if not cfg.google_oauth_client_id or not cfg.google_oauth_client_secret:
        raise OAuthError("not_configured", retryable=False)

    response = await _post_async(
        _TOKEN_URL,
        {
            "code": code,
            "client_id": cfg.google_oauth_client_id,
            "client_secret": cfg.google_oauth_client_secret,
            "redirect_uri": redirect_uri or cfg.google_oauth_redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    bundle = _bundle_from(_raise_for_error(response), fallback_scopes=CALENDAR_SCOPE)
    if bundle.refresh_token is None:
        raise OAuthError("no_refresh_token", retryable=False)
    return bundle


async def refresh_access_token(refresh_token: str, *, known_scopes: str) -> TokenBundle:
    """refresh token → 새 access token.

    응답에 `refresh_token` 이 없는 게 정상이므로 `TokenBundle.refresh_token` 은 None 일
    수 있다 — 호출자는 기존 값을 유지한다.
    """
    cfg = get_settings()
    if not cfg.google_oauth_client_id or not cfg.google_oauth_client_secret:
        raise OAuthError("not_configured", retryable=False)

    response = await _post_async(
        _TOKEN_URL,
        {
            "refresh_token": refresh_token,
            "client_id": cfg.google_oauth_client_id,
            "client_secret": cfg.google_oauth_client_secret,
            "grant_type": "refresh_token",
        },
    )
    return _bundle_from(_raise_for_error(response), fallback_scopes=known_scopes)


async def revoke(token: str) -> None:
    """Google 쪽 권한 회수 — best-effort.

    실패해도 예외를 올리지 않는다. 우리 DB 의 `revoked_at` 을 찍는 것이 사용자에게 보이는
    '연결 해제'이고, 원격 회수는 그 뒤에 하는 정리다. 여기서 던지면 네트워크가 흔들릴 때
    사용자가 연결을 **해제조차 못 하게** 된다.
    """
    try:
        await _post_async(_REVOKE_URL, {"token": token})
    except OAuthError as exc:
        logger.info("calendar_revoke_failed", extra={"reason": exc.reason})
