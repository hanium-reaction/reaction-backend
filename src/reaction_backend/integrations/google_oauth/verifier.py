"""Google id_token 검증 — Issue #16.

- staging/prod: `google-auth` 가 id_token 의 서명 + `iss` + `aud` + `exp` 를 한 번에 검증.
  `iss` 는 라이브러리(`verify_oauth2_token`)가 `accounts.google.com` /
  `https://accounts.google.com` 둘 다 이미 허용한다 — 이 모듈이 따로 검사하지 않는다.
  `aud` 는 웹·Android 두 OAuth Client 를 모두 허용한다(#322) — `verify_oauth2_token` 의
  `audience` 인자가 `str | list[str]` 를 받으므로 설정된 client_id 들을 리스트로 넘긴다.
- local: `AUTH_STUB_MODE=true` 시 Google 호출 우회하고 demo 클레임 반환.
  - 기본: 고정 demo 계정 (시드 시나리오 계정과 매칭).
  - `id_token="demo:<id>"`: 브라우저별 격리 데모 계정 — staging 데모에서 테스터
    전원이 한 계정을 공유하며 인터뷰 세션/동시성 lock 이 충돌하는 문제를 푼다.
    FE 는 localStorage 에 랜덤 id 를 저장해 `demo:<id>` 로 보내면 된다.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from google.auth import exceptions as g_exceptions
from google.auth import jwt as g_jwt
from google.auth.transport import requests as g_requests
from google.oauth2 import id_token as g_id_token

from reaction_backend.config import get_settings
from reaction_backend.schemas.errors import ApiError, ErrorCode

logger = logging.getLogger(__name__)

# google-auth 예외 메시지 → 분류. 메시지를 그대로 찍지 않는 이유는 `_rejection_summary` 참조.
_REJECTION_KINDS: tuple[tuple[str, str], ...] = (
    ("Token expired", "expired"),
    ("Token used too early", "too_early"),
    ("wrong audience", "wrong_audience"),
    ("verify token signature", "bad_signature"),
    ("Wrong issuer", "wrong_issuer"),
    ("Certificate for key id", "unknown_key"),  # 구글 키 교체 직후
    ("does not contain required claim", "malformed"),
    ("segment", "malformed"),
    ("padding", "malformed"),
)


# Google 공개키(certs) 조회 타임아웃(초). google-auth 기본값은 120초다 — 인증서 엔드포인트가
# 느리면 로그인 한 건이 2분을 붙잡았다. 로그인 화면에서 기다릴 수 있는 만큼만 기다리고, 넘으면
# "잠시 후 다시" 로 돌려보낸다(아래 TransportError 분기).
_CERT_FETCH_TIMEOUT_SECONDS = 5.0


def _request_with_timeout(
    url: str,
    method: str = "GET",
    body: Any = None,
    headers: Any = None,
    timeout: float | None = None,  # noqa: ARG001 — 라이브러리 기본(120s)을 무시하고 상한을 건다
    **kwargs: Any,
) -> Any:
    """`google.auth.transport.Request` 호출 규약을 그대로 받되 타임아웃만 짧게 강제한다.

    `verify_oauth2_token` 은 이 콜러블을 `request(certs_url, method="GET")` 로만 부른다.
    호출마다 새 `Request`(= 새 requests.Session) — 검증은 `asyncio.to_thread` 워커에서 돌아
    스레드 간 세션 공유를 피한다.
    """
    request = g_requests.Request()
    return request(
        url,
        method=method,
        body=body,
        headers=headers,
        timeout=_CERT_FETCH_TIMEOUT_SECONDS,
        **kwargs,
    )


def _client_tail(client_id: str) -> str:
    """client_id 를 로그에서 구분할 만큼만 — `…03rs` (앞부분·도메인 생략)."""
    return "…" + client_id.split(".", 1)[0][-4:] if client_id else "(unset)"


def _rejection_summary(token: str, error: ValueError, audiences: list[str]) -> str:
    """id_token 검증 실패를 **토큰 없이** 한 줄로 요약한다 — 운영 로그로 원인을 가르려고.

    예전엔 사유를 남기지 않아, 스테이징 로그인 401 이 aud 불일치인지 만료인지 시계 오차인지
    로그로 가를 수 없었다(2026-09-16).

    - google-auth 의 예외 메시지는 형식 오류일 때 **토큰 조각을 그대로 담는다** — 원문을 찍지
      않고 종류(kind)로만 분류한다.
    - 서명 검증 없이 클레임만 풀어 aud·만료·발급 시각을 남긴다. `sub`·`email` 은 남기지 않는다.
    - client_id 는 끝 4자만 — 설정값과 토큰의 aud 가 다른지 눈으로 대조할 수 있으면 충분하다.
    """
    message = str(error)
    kind = next((k for needle, k in _REJECTION_KINDS if needle in message), "other")
    expected = ",".join(_client_tail(a) for a in audiences)
    try:
        # 서명·aud·만료를 **보지 않고** 페이로드만 푼다 — 위 검증이 이미 실패한 토큰이다.
        claims: dict[str, Any] = g_jwt.decode(token, verify=False)  # type: ignore[no-untyped-call]
    except (ValueError, TypeError):
        return f"kind=malformed expected_aud={expected}"

    now = time.time()
    aud = claims.get("aud")
    if aud in audiences:
        aud_label = "match"
    elif isinstance(aud, str):
        aud_label = f"mismatch({_client_tail(aud)})"
    else:
        aud_label = "missing"
    parts = [f"kind={kind}", f"aud={aud_label}", f"expected_aud={expected}"]
    exp, iat = claims.get("exp"), claims.get("iat")
    if isinstance(exp, int | float):
        parts.append(f"exp_in={int(exp - now)}s")
    if isinstance(iat, int | float):
        parts.append(f"iat_ago={int(now - iat)}s")
    parts.append(f"iss={claims.get('iss')}")
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class GoogleClaims:
    """검증된 Google id_token 의 핵심 클레임."""

    sub: str  # Google account ID (안정 식별자)
    email: str
    name: str


# stub 모드용 고정 클레임 — DEMO_USER 와 email 매칭.
_STUB_CLAIMS = GoogleClaims(
    sub="google-demo-sub",
    email="demo@reaction.local",
    name="김민수",
)

# 브라우저별 데모 계정 opt-in 접두사. 그 외 토큰은 전부 고정 계정(하위호환).
_STUB_DEVICE_PREFIX = "demo:"
_STUB_SLUG_STRIP = re.compile(r"[^a-z0-9_-]")


def _stub_claims(token: str) -> GoogleClaims:
    """stub 모드 클레임 결정.

    - `demo:<id>` → id 를 slug 로 정규화해 격리된 데모 계정 클레임 생성.
    - 그 외("stub" 등 기존 값 포함) → 고정 demo 계정 — 시드 데이터 계정 유지.
    """
    if token.startswith(_STUB_DEVICE_PREFIX):
        slug = _STUB_SLUG_STRIP.sub("", token[len(_STUB_DEVICE_PREFIX) :].lower())[:32]
        if slug:
            return GoogleClaims(
                sub=f"google-demo-{slug}",
                email=f"demo+{slug}@reaction.local",
                name=f"데모 {slug[:8]}",
            )
    return _STUB_CLAIMS


def verify_google_id_token(token: str) -> GoogleClaims:
    """id_token 을 검증하고 클레임을 반환한다.

    Raises:
        ApiError(AUTH_INVALID_ID_TOKEN, 401): 서명/만료/aud 불일치/형식 오류.
        ApiError(COMMON_INTERNAL_ERROR, 503): Google 공개키 조회 실패·지연 — 토큰 탓이 아니다.
        RuntimeError: CLIENT_ID 미설정 + stub mode 도 꺼진 misconfig.

    **동기(블로킹) 함수다** — Google 공개키를 HTTPS 로 가져온다. async 핸들러에서는
    `asyncio.to_thread` 로 불러야 이벤트 루프(단일 워커)를 막지 않는다.
    """
    cfg = get_settings()

    if cfg.auth_stub_mode:
        return _stub_claims(token)

    if not cfg.google_oauth_client_id:
        raise RuntimeError(
            "GOOGLE_OAUTH_CLIENT_ID is not configured. "
            "Set it, or enable AUTH_STUB_MODE for local development."
        )

    # 웹 client_id 는 항상 허용, Android client_id 는 설정된 경우에만 추가(#322) —
    # 미설정 시 기존 동작(웹 하나만 허용)과 완전히 동일하다.
    audiences = [cfg.google_oauth_client_id]
    if cfg.google_oauth_android_client_id:
        audiences.append(cfg.google_oauth_android_client_id)

    try:
        # google-auth 함수가 py.typed 미배포 — mypy strict 에서 no-untyped-call 발생.
        info: dict[str, Any] = g_id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            token,
            _request_with_timeout,
            audience=audiences,
        )
    except g_exceptions.TransportError as e:
        # 공개키 조회가 실패·지연됐다 — 토큰은 멀쩡할 수 있다. ValueError 가 아니라서 예전엔
        # 500 으로 샜다. 로그에는 예외 종류만(메시지에 URL·내부 정보가 섞일 수 있다).
        logger.warning("google_id_token_cert_fetch_failed error=%s", type(e).__name__)
        raise ApiError(
            ErrorCode.COMMON_INTERNAL_ERROR,
            "Google 로그인 확인이 잠시 늦어지고 있어요. 잠시 후 다시 시도해 주세요.",
            http_status=HTTPStatus.SERVICE_UNAVAILABLE,
        ) from e
    except ValueError as e:
        logger.warning("google_id_token_rejected %s", _rejection_summary(token, e, audiences))
        raise ApiError(
            ErrorCode.AUTH_INVALID_ID_TOKEN,
            "Google 로그인 토큰이 유효하지 않습니다.",
            http_status=HTTPStatus.UNAUTHORIZED,
        ) from e

    sub = info.get("sub")
    email = info.get("email")
    name = info.get("name") or info.get("given_name") or ""
    if not isinstance(sub, str) or not sub or not isinstance(email, str) or not email:
        logger.warning(
            "google_id_token_rejected kind=missing_claims has_sub=%s has_email=%s",
            bool(sub),
            bool(email),
        )
        raise ApiError(
            ErrorCode.AUTH_INVALID_ID_TOKEN,
            "Google 토큰에 필요한 클레임이 없습니다.",
            http_status=HTTPStatus.UNAUTHORIZED,
        )
    return GoogleClaims(sub=sub, email=email, name=str(name))
