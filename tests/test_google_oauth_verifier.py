"""Google id_token 검증 — 웹/Android 두 client_id 허용 (#322, FE #237 §3).

Android 네이티브 로그인(Credential Manager)으로 전환하면 id_token 의 `aud` 가 웹이 아닌
Android OAuth Client 것이 된다. `verify_google_id_token` 이 두 client_id 를 모두 허용하는지,
그리고 Android client_id 가 미설정이면 기존처럼 웹 하나만 허용하는지(하위호환) 를 고정한다.

`AUTH_STUB_MODE=true` 는 conftest 의 autouse fixture 가 항상 켜두므로, 이 테스트들은 실
검증 경로를 타기 위해 명시적으로 꺼야 한다. `google.oauth2.id_token.verify_oauth2_token` 은
네트워크(Google 인증서 fetch)를 타므로 monkeypatch 로 대체한다.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any
from unittest.mock import patch

import pytest

from reaction_backend.config import get_settings
from reaction_backend.integrations.google_oauth.verifier import verify_google_id_token
from reaction_backend.schemas.errors import ApiError


@pytest.fixture
def _real_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """stub 모드를 끄고 실 검증 경로(Google 라이브러리 호출)를 태운다."""
    monkeypatch.setenv("AUTH_STUB_MODE", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fake_idinfo() -> dict[str, Any]:
    return {"sub": "google-sub-1", "email": "user@example.com", "name": "홍길동"}


def test_audience_is_web_client_id_only_when_android_unset(
    _real_verification: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "web-client-id")
    monkeypatch.delenv("GOOGLE_OAUTH_ANDROID_CLIENT_ID", raising=False)
    get_settings.cache_clear()

    with patch(
        "reaction_backend.integrations.google_oauth.verifier.g_id_token.verify_oauth2_token",
        return_value=_fake_idinfo(),
    ) as mock_verify:
        verify_google_id_token("token")

    assert mock_verify.call_args.kwargs["audience"] == ["web-client-id"]


def test_audience_includes_both_web_and_android_client_id(
    _real_verification: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "web-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_ANDROID_CLIENT_ID", "android-client-id")
    get_settings.cache_clear()

    with patch(
        "reaction_backend.integrations.google_oauth.verifier.g_id_token.verify_oauth2_token",
        return_value=_fake_idinfo(),
    ) as mock_verify:
        claims = verify_google_id_token("token")

    assert mock_verify.call_args.kwargs["audience"] == ["web-client-id", "android-client-id"]
    assert claims.sub == "google-sub-1"
    assert claims.email == "user@example.com"


def test_missing_web_client_id_raises_regardless_of_android(
    _real_verification: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """웹 client_id 가 비어 있으면 Android 만 설정돼 있어도 misconfig 로 취급한다."""
    # delenv 가 아니라 빈 값 — 지우면 pydantic-settings 가 개발자 `.env` 의 값을 읽어
    # 로컬에서 Google 을 설정해 둔 사람만 이 테스트가 깨졌다(환경 변수가 `.env` 보다 우선).
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "")
    monkeypatch.setenv("GOOGLE_OAUTH_ANDROID_CLIENT_ID", "android-client-id")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="GOOGLE_OAUTH_CLIENT_ID"):
        verify_google_id_token("token")


# ── 검증 실패 사유 로그 (2026-09-16 스테이징 401 을 로그로 가를 수 없었다) ─────────

_VERIFY = "reaction_backend.integrations.google_oauth.verifier.g_id_token.verify_oauth2_token"
_WEB_ID = "5836281269-abcd03rs.apps.googleusercontent.com"
_EMAIL = "someone@example.com"
_SUB = "google-sub-private-123"


def _unsigned_jwt(payload: dict[str, Any]) -> str:
    def seg(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = seg(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    return f"{header}.{seg(json.dumps(payload).encode())}.{seg(b'signature')}"


def _rejected_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    token: str,
    error: ValueError,
) -> str:
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", _WEB_ID)
    monkeypatch.delenv("GOOGLE_OAUTH_ANDROID_CLIENT_ID", raising=False)
    get_settings.cache_clear()
    caplog.set_level(logging.WARNING, logger="reaction_backend.integrations.google_oauth.verifier")

    with patch(_VERIFY, side_effect=error), pytest.raises(ApiError):
        verify_google_id_token(token)

    lines = [r.getMessage() for r in caplog.records if "google_id_token_rejected" in r.getMessage()]
    assert len(lines) == 1, lines
    return lines[0]


def test_wrong_audience_is_logged_with_both_client_tails(
    _real_verification: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """설정된 client 와 토큰의 aud 가 다르면 **둘 다 끝 4자**가 남는다 — 눈으로 대조할 수 있게."""
    other = "111111111111-zzzzabcd.apps.googleusercontent.com"
    now = int(time.time())
    token = _unsigned_jwt(
        {
            "aud": other,
            "iss": "https://accounts.google.com",
            "exp": now + 3000,
            "iat": now - 5,
            "email": _EMAIL,
            "sub": _SUB,
        }
    )

    line = _rejected_log(
        monkeypatch, caplog, token, ValueError(f"Token has wrong audience {other}, expected ...")
    )

    assert "kind=wrong_audience" in line
    assert "aud=mismatch(…abcd)" in line
    assert "expected_aud=…03rs" in line
    assert "exp_in=" in line and "iat_ago=" in line
    # 토큰·개인 식별자는 남기지 않는다
    for secret in (token, _EMAIL, _SUB):
        assert secret not in line


def test_expired_token_is_logged_as_expired(
    _real_verification: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now = int(time.time())
    token = _unsigned_jwt(
        {"aud": _WEB_ID, "iss": "accounts.google.com", "exp": now - 120, "iat": now - 3720}
    )

    line = _rejected_log(monkeypatch, caplog, token, ValueError("Token expired, 1 < 2"))

    assert "kind=expired" in line
    assert "aud=match" in line
    assert "exp_in=-1" in line  # -120s 근처


def test_malformed_token_never_reaches_the_log(
    _real_verification: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """google-auth 는 형식 오류 메시지에 토큰 조각을 담는다 — 그 메시지를 그대로 찍지 않는다."""
    token = "not-a-jwt-but-looks-SECRET-0123456789"

    line = _rejected_log(
        monkeypatch, caplog, token, ValueError(f"Wrong number of segments in token: {token!r}")
    )

    assert "kind=malformed" in line
    assert token not in line and "SECRET" not in line


def test_missing_claims_are_logged_without_values(
    _real_verification: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", _WEB_ID)
    get_settings.cache_clear()
    caplog.set_level(logging.WARNING, logger="reaction_backend.integrations.google_oauth.verifier")

    with patch(_VERIFY, return_value={"sub": _SUB, "email": ""}), pytest.raises(ApiError):
        verify_google_id_token("token")

    text = caplog.text
    assert "google_id_token_rejected kind=missing_claims has_sub=True has_email=False" in text
    assert _SUB not in text
