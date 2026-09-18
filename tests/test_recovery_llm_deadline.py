"""회복 제안 LLM 대기 상한 (recovery-13).

재시도는 시도마다 `timeout` 을 새로 줘서, 회복 personalize(`timeout=12.0`)가 기본 재시도
3회를 타면 12s×3+backoff ≈ 37초 동안 사용자가 로딩만 봤다. `max_attempts` addendum 이
그 상한을 호출별로 묶는지, 기본값은 종전 그대로인지 고정한다.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from reaction_backend.config import get_settings
from reaction_backend.llm import tool_executor
from tests.conftest import DEMO_USER_UUID, FakeActionItemRepo, FakeRecoveryRepo
from tests.test_recovery import _generate, _seed_action


class _Schema(BaseModel):
    text: str = ""


class _Tmpl:
    prompt_id = "test/deadline"
    version = "v1"


def _count_slow_provider(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """매 시도가 timeout 으로 끝나는 provider — 몇 번 불렸는지 센다."""
    calls = {"n": 0}
    monkeypatch.setattr(
        tool_executor.prompt_registry, "render", lambda pid, variables: ("프롬프트", _Tmpl())
    )

    async def _slow(**kwargs: Any) -> Any:
        calls["n"] += 1
        raise TimeoutError

    monkeypatch.setattr(tool_executor, "generate_structured", _slow)
    return calls


async def test_max_attempts_one_stops_after_a_single_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _count_slow_provider(monkeypatch)

    result = await tool_executor.aiClient.run(
        module="recovery",
        schema=_Schema,
        prompt_id="test/deadline",
        fallback=_Schema(text="룰"),
        timeout=0.01,
        max_attempts=1,
    )

    assert calls["n"] == 1
    assert result.fell_back is True
    assert result.reason == "timeout"
    assert result.value.text == "룰"


async def test_default_keeps_the_configured_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _count_slow_provider(monkeypatch)

    result = await tool_executor.aiClient.run(
        module="recovery",
        schema=_Schema,
        prompt_id="test/deadline",
        fallback=_Schema(text="룰"),
        timeout=0.01,
    )

    assert calls["n"] == max(1, get_settings().llm_max_retries)
    assert result.fell_back is True


def test_recovery_generate_does_not_retry_the_llm(
    client: TestClient,
    fake_recovery_repo: FakeRecoveryRepo,
    fake_action_item_repo: FakeActionItemRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """회복 personalize 는 12초 1회 — 실패하면 곧바로 룰 카드를 낸다."""
    from reaction_backend.llm import RunResult, aiClient

    captured: dict[str, Any] = {}

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        captured.update(kwargs)
        return RunResult(
            value=kwargs["fallback"](),
            fell_back=True,
            reason="timeout",
            prompt_id=kwargs["prompt_id"],
            prompt_version="v2",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)

    action = _seed_action(fake_action_item_repo, title="GROUP BY 실습")
    execution = fake_recovery_repo.register_execution(
        user_id=DEMO_USER_UUID,
        action_item_id=action.id,
        completion_status="failed",
        failure_tags=["AMBIGUITY"],
    )
    resp = _generate(client, f"exec_{execution.id}")
    assert resp.status_code == 201, resp.json()
    assert resp.json()["aiSource"] == "rule"

    assert captured["module"] == "recovery"
    assert captured["max_attempts"] == 1
    assert captured["timeout"] <= 12.0
