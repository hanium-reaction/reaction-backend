"""Settings 부팅 가드 — AUTH_STUB_MODE 는 staging/prod 에서 금지 (Issue #94).

stub 모드는 Google id_token 검증을 건너뛰고 고정 demo 클레임을 반환 = 인증 우회.
배포 환경(staging/prod)에 켜지면 모든 로그인이 한 유저로 붕괴하므로 부팅을 실패시킨다.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from reaction_backend.config import Settings


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_auth_stub_mode_forbidden_in_deployed_envs(env: str) -> None:
    """staging/prod + AUTH_STUB_MODE=true → 부팅 실패 (ValidationError)."""
    with pytest.raises(ValidationError, match="AUTH_STUB_MODE"):
        Settings(app_env=env, auth_stub_mode=True)


@pytest.mark.parametrize("env", ["local", "dev"])
def test_auth_stub_mode_allowed_in_dev_envs(env: str) -> None:
    """local/dev 는 기존대로 stub 허용 — 부팅 OK."""
    cfg = Settings(app_env=env, auth_stub_mode=True)
    assert cfg.auth_stub_mode is True


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_auth_stub_off_boots_in_deployed_envs(env: str) -> None:
    """staging/prod 도 AUTH_STUB_MODE=false 면 정상 부팅 (실검증 경로)."""
    cfg = Settings(app_env=env, auth_stub_mode=False)
    assert cfg.auth_stub_mode is False
    assert cfg.app_env == env
