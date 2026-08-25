"""Google id_token 검증 — 웹/Android 두 client_id 허용 (#322, FE #237 §3).

Android 네이티브 로그인(Credential Manager)으로 전환하면 id_token 의 `aud` 가 웹이 아닌
Android OAuth Client 것이 된다. `verify_google_id_token` 이 두 client_id 를 모두 허용하는지,
그리고 Android client_id 가 미설정이면 기존처럼 웹 하나만 허용하는지(하위호환) 를 고정한다.

`AUTH_STUB_MODE=true` 는 conftest 의 autouse fixture 가 항상 켜두므로, 이 테스트들은 실
검증 경로를 타기 위해 명시적으로 꺼야 한다. `google.oauth2.id_token.verify_oauth2_token` 은
네트워크(Google 인증서 fetch)를 타므로 monkeypatch 로 대체한다.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from reaction_backend.config import get_settings
from reaction_backend.integrations.google_oauth.verifier import verify_google_id_token


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
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.setenv("GOOGLE_OAUTH_ANDROID_CLIENT_ID", "android-client-id")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="GOOGLE_OAUTH_CLIENT_ID"):
        verify_google_id_token("token")
