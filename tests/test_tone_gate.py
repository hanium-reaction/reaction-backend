"""톤·구조 게이트 회귀 (근거 대장 §4 S6 — `banned_words` 뒤에 붙는 결정적 검증기).

`banned_words` 는 명사 1:1 치환이라 문장 구조 문제(사람 귀인·자존감 부양)를 못 잡는다.
이 필터가 조용히 죽으면(no-op 이 되면) 그 문장들이 그대로 회복 카드에 실린다 — 그
사고를 여기서 잡는다.
"""

from __future__ import annotations

import pytest

from reaction_backend.safety.tone_gate import (
    PERSON_ATTRIBUTION_MARKERS,
    SELF_ESTEEM_BOOST_MARKERS,
    check_structured,
    scan,
)


@pytest.mark.parametrize("marker", PERSON_ATTRIBUTION_MARKERS)
def test_every_person_attribution_marker_is_caught(marker: str) -> None:
    text = f"{marker} 안 해서 이렇게 됐어요"
    assert marker in scan(text), f"'{marker}' 가 있는데 scan 이 못 잡는다"


@pytest.mark.parametrize("marker", SELF_ESTEEM_BOOST_MARKERS)
def test_every_self_esteem_boost_marker_is_caught(marker: str) -> None:
    text = f"{marker}요, 이번에도 잘 될 거예요"
    assert marker in scan(text), f"'{marker}' 가 있는데 scan 이 못 잡는다"


def test_clean_coaching_text_is_untouched() -> None:
    """정상적인 청유형 코칭 카드 문구는 걸리지 않는다 — 오탐이 톤을 망치지 않는다."""
    text = "책상에 앉으면 GROUP BY 실습 예제 1절만 15분 해볼까요?"
    assert scan(text) == ()


def test_scan_preserves_first_seen_order_and_dedupes() -> None:
    text = "당신이 그랬잖아요, 당신이 정말 똑똑하시니까 다시 해봐요"
    hits = scan(text)
    assert hits == ("당신이", "똑똑하")  # 중복 제거, 최초 등장 순서


class TestCheckStructured:
    def test_flags_nested_dict_and_list_strings(self) -> None:
        payload = {
            "if_clause": "책상에 앉으면",
            "then_clause": "네가 안 해서 이렇게 됐잖아요",
            "tags": ["정상", "역시 잘하시네요"],
            "meta": {"nested": {"deep": "괜찮아요"}},
            "minutes": -30,
        }
        blocked, hits = check_structured(payload)

        assert blocked is True
        assert set(hits) >= {"네가", "역시 잘하"}

    def test_does_not_mutate_or_substitute(self) -> None:
        """banned_words 와 달리 치환하지 않는다 — 반환값에 정제된 트리가 없다."""
        payload = {"rationale": "당신이 게을러서 그래요"}
        blocked, hits = check_structured(payload)

        assert blocked is True
        assert hits == ("당신이",)
        # 원본 payload 는 그대로 — check_structured 는 dict 를 반환하지 않는다.
        assert payload["rationale"] == "당신이 게을러서 그래요"

    def test_clean_payload_is_not_blocked(self) -> None:
        payload = {
            "if_clause": "저녁 먹고 책상에 앉으면",
            "then_clause": "GROUP BY 실습 예제 1절만 15분 해볼까요",
            "rationale": "이 작업이 좀 컸던 것 같아요. 절반만 해볼까요.",
            "estimated_workload_change_minutes": -30,
        }
        blocked, hits = check_structured(payload)

        assert blocked is False
        assert hits == ()

    def test_ignores_non_string_leaves(self) -> None:
        blocked, hits = check_structured({"minutes": -30, "flag": True, "empty": None})
        assert blocked is False
        assert hits == ()


# ── 실 경로 통합: aiClient.run() 이 실제로 이 게이트를 거친다 ──────────────


async def test_aiclient_run_falls_back_when_provider_output_violates_tone_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider 가 성공적으로 값을 돌려줘도, 톤 게이트에 걸리면 fallback 으로 분기한다.

    banned_words 의 `blocked` 경로(HARD_BLOCK_TERMS 가 비어 있어 사실상 도달 불가)와
    달리, 이 게이트는 실제로 도달 가능해야 한다 — 그래서 provider 호출 자체를 모킹해
    "성공했지만 위반" 상황을 만들어 `aiClient.run()` 전체 경로로 검증한다.
    """
    from pydantic import BaseModel

    from reaction_backend.llm import aiClient
    from reaction_backend.llm.provider import ProviderResponse

    class _Card(BaseModel):
        if_clause: str
        then_clause: str

    async def fake_generate_structured(**kwargs: object) -> tuple[_Card, ProviderResponse]:
        value = _Card(if_clause="책상에 앉으면", then_clause="당신이 못해서 그런 거예요")
        return value, ProviderResponse(raw_text="{}", tokens_in=10, tokens_out=5, model="fake")

    monkeypatch.setattr(
        "reaction_backend.llm.tool_executor.generate_structured", fake_generate_structured
    )

    result = await aiClient.run(
        module="recovery",
        schema=_Card,
        prompt_id="recovery/if_then_proposal",
        fallback=lambda: _Card(if_clause="", then_clause="안전한 폴백 문구"),
        variables={
            "strategy_label": "x",
            "strategy_group": "DOWNSCOPE",
            "base_template": "x",
            "failure_type": "x",
            "confidence": "n/a",
            "interruption_summary": "없음",
            "context_summary": "x",
        },
        timeout=1.0,
    )

    assert result.fell_back is True
    assert result.reason == "tone_gate"
    assert result.value.then_clause == "안전한 폴백 문구"
    assert "당신이" in result.banned_hits


async def test_aiclient_run_returns_provider_value_when_tone_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import BaseModel

    from reaction_backend.llm import aiClient
    from reaction_backend.llm.provider import ProviderResponse

    class _Card(BaseModel):
        if_clause: str
        then_clause: str

    async def fake_generate_structured(**kwargs: object) -> tuple[_Card, ProviderResponse]:
        value = _Card(if_clause="책상에 앉으면", then_clause="5분만 해볼까요")
        return value, ProviderResponse(raw_text="{}", tokens_in=10, tokens_out=5, model="fake")

    monkeypatch.setattr(
        "reaction_backend.llm.tool_executor.generate_structured", fake_generate_structured
    )

    result = await aiClient.run(
        module="recovery",
        schema=_Card,
        prompt_id="recovery/if_then_proposal",
        fallback=lambda: _Card(if_clause="", then_clause="폴백"),
        variables={
            "strategy_label": "x",
            "strategy_group": "DOWNSCOPE",
            "base_template": "x",
            "failure_type": "x",
            "confidence": "n/a",
            "interruption_summary": "없음",
            "context_summary": "x",
        },
        timeout=1.0,
    )

    assert result.fell_back is False
    assert result.value.then_clause == "5분만 해볼까요"
