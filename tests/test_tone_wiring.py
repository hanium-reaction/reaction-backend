"""톤 prefix 배선 — #23-C/#23-D.

#23-C: tool_executor 가 렌더 직후 톤 prefix 를 prompt_text 에 선행시키는지 + cron 전달.
#23-D: LangGraph(interview/first_plan) 노드·runner 가 config["configurable"]["tone_mode"]
       경로로 톤을 aiClient.run 에 전달하는지. provider 는 mock 으로 가용성 무관하게 테스트.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from reaction_backend.llm import tool_executor
from reaction_backend.llm.prompt_compose import TONE_SYSTEM_PREFIXES
from reaction_backend.llm.provider import ProviderUnavailable
from reaction_backend.orchestrator import first_plan, interview, interview_runner
from reaction_backend.schemas.today import MorningBriefDraft
from tests.conftest import DEMO_USER_UUID, FakeActionItemRepo, FakeDailyBriefRepo, _FakeSession


class _Schema(BaseModel):
    pass


class _Tmpl:
    prompt_id = "test/tone"
    version = "v1"


def _patch_render(monkeypatch: pytest.MonkeyPatch, body: str = "원문 프롬프트") -> dict[str, Any]:
    """prompt_registry.render → 고정 (body, tmpl). generate_structured → prompt 캡처 후 fallback."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        tool_executor.prompt_registry, "render", lambda pid, variables: (body, _Tmpl())
    )

    async def _fake_gen(
        *, schema: Any, prompt_text: str, timeout: float, thinking_budget: int | None = None
    ) -> Any:
        captured["prompt"] = prompt_text
        raise ProviderUnavailable("no key (test)")

    monkeypatch.setattr(tool_executor, "generate_structured", _fake_gen)
    return captured


# ───────────────────────── tool_executor 메커니즘 ─────────────────────────


@pytest.mark.asyncio
async def test_run_prepends_tone_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_render(monkeypatch)
    result = await tool_executor.aiClient.run(
        module="inbox",
        schema=_Schema,
        prompt_id="test/tone",
        fallback=lambda: _Schema(),
        tone_mode="gentle",
    )
    assert result.fell_back  # provider mock 이 unavailable → fallback
    assert captured["prompt"].startswith(TONE_SYSTEM_PREFIXES["gentle"])
    assert "원문 프롬프트" in captured["prompt"]


@pytest.mark.asyncio
async def test_run_no_tone_keeps_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_render(monkeypatch)
    await tool_executor.aiClient.run(
        module="inbox",
        schema=_Schema,
        prompt_id="test/tone",
        fallback=lambda: _Schema(),
        tone_mode=None,
    )
    assert captured["prompt"] == "원문 프롬프트"  # prefix 없음 = 기존 동작


@pytest.mark.asyncio
async def test_run_unknown_tone_keeps_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_render(monkeypatch)
    await tool_executor.aiClient.run(
        module="inbox",
        schema=_Schema,
        prompt_id="test/tone",
        fallback=lambda: _Schema(),
        tone_mode="bogus",
    )
    assert captured["prompt"] == "원문 프롬프트"  # 미지원 값 → prefix 없음


# ───────────────────────── morning_brief cron 전달 ─────────────────────────


@pytest.mark.asyncio
async def test_morning_brief_threads_tone(monkeypatch: pytest.MonkeyPatch) -> None:
    from reaction_backend.scheduler import morning_brief

    captured: dict[str, Any] = {}

    async def _fake_run(**kwargs: Any) -> Any:
        captured["tone_mode"] = kwargs.get("tone_mode")
        draft = MorningBriefDraft(
            headline_ko="좋은 아침이에요",
            first_step="첫 걸음",
            reason_why_now="지금이 좋아요",
            adjustment_hints=[],
        )
        return SimpleNamespace(value=draft, fell_back=True)

    monkeypatch.setattr(morning_brief.aiClient, "run", _fake_run)

    await morning_brief.run_morning_brief_for_user(
        DEMO_USER_UUID,
        datetime(2026, 6, 2, 6, 0, tzinfo=UTC),
        action_repo=FakeActionItemRepo(),
        brief_repo=FakeDailyBriefRepo(),
        session=_FakeSession(),
        tone_mode="strict",
    )
    assert captured["tone_mode"] == "strict"


# ───────────────────────── #23-D LangGraph (config 경로) ─────────────────────────


def _capture_run(monkeypatch: pytest.MonkeyPatch, target: Any) -> dict[str, Any]:
    """target.aiClient.run 을 캡처용 mock 으로 교체 (값은 None, fell_back=False)."""
    captured: dict[str, Any] = {}

    async def _fake(**kwargs: Any) -> Any:
        captured["tone_mode"] = kwargs.get("tone_mode")
        return SimpleNamespace(value=None, fell_back=False)

    monkeypatch.setattr(target.aiClient, "run", _fake)
    return captured


@pytest.mark.asyncio
async def test_interview_node_threads_tone(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_run(monkeypatch, interview)
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    config = {"configurable": {"session": None, "tone_mode": "encouraging"}}
    await interview.ask_question(state, config)
    assert captured["tone_mode"] == "encouraging"


@pytest.mark.asyncio
async def test_interview_runner_passes_tone(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_run(monkeypatch, interview)
    await interview_runner.start_interview(
        session_id=uuid4(), user_id=uuid4(), session=None, tone_mode="strict"
    )
    assert captured["tone_mode"] == "strict"


@pytest.mark.asyncio
async def test_first_plan_node_threads_tone(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_run(monkeypatch, first_plan)
    state: dict[str, Any] = {
        "user_id": uuid4(),
        "planning_context": {"prompt_vars": {}},
        "used_fallback": False,
    }
    config = {"configurable": {"session": None, "tone_mode": "gentle"}}
    await first_plan.decompose_goal(state, config)  # type: ignore[arg-type]
    assert captured["tone_mode"] == "gentle"


@pytest.mark.asyncio
async def test_node_no_tone_in_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """config 에 tone 없으면 None 전달 (= 기존 동작)."""
    captured = _capture_run(monkeypatch, interview)
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    await interview.ask_question(state, {"configurable": {"session": None}})
    assert captured["tone_mode"] is None
