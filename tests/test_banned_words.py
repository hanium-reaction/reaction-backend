"""금지어 후처리 필터 회귀 (DevBaseline §4.2 잠금 · #20 DoD 8).

이 필터는 잠금 결정("Be on your side, not on your case" 톤 강제 · AGENTS.md §1)인데
**회귀 테스트가 0건**이었다 — `enforce()` 를 no-op 으로 만들어도 전 스위트가 통과했다.
사용자에게 나가는 모든 LLM 문자열이 여기를 거치므로, 필터가 조용히 죽으면 비난 어휘가
그대로 회복 카드에 실린다. 그 사고를 여기서 잡는다.

⚠️ 이 파일은 필터의 **현재 한계도 함께 고정**한다(활용형 미포착·치환 비문). 감추면
"필터가 완벽하다"는 착각이 굳고, 고칠 때 무엇이 바뀌는지도 알 수 없다. 한계를 개선하면
해당 테스트가 실패하므로 의식적으로 갱신하게 된다.
"""

from __future__ import annotations

import pytest

from reaction_backend.safety.banned_words import (
    BANNED_REPLACEMENTS,
    enforce,
    enforce_many,
    enforce_structured,
    scan,
)

# ── 핵심 계약: 사전의 모든 키가 실제로 치환된다 ──────────────────────────


@pytest.mark.parametrize("banned", list(BANNED_REPLACEMENTS))
def test_every_dictionary_entry_is_actually_replaced(banned: str) -> None:
    """사전에 있는 **모든** 금지어가 실제로 잡히고 치환된다.

    사전에 키를 추가했는데 패턴 컴파일이 안 따라오는(예: 정규식 이스케이프 누락) 사고를
    개별 키 단위로 잡는다. `enforce` 를 no-op 으로 만들면 여기서 전부 터진다.
    """
    text = f"어제는 {banned} 상태였어요"
    result = enforce(text)

    assert banned in result.hits, f"'{banned}' 가 사전에 있는데 scan 이 못 잡는다"
    assert result.changed
    assert BANNED_REPLACEMENTS[banned] in result.text
    # 치환어 자체가 다른 금지어를 포함하지 않는다(무한 치환·재매칭 방지).
    assert not scan(BANNED_REPLACEMENTS[banned]), (
        f"치환어 '{BANNED_REPLACEMENTS[banned]}' 가 또 다른 금지어를 포함한다"
    )


def test_longest_key_wins_over_substring() -> None:
    """부분문자열 충돌 시 긴 키가 이긴다 — '실패율'이 '실패'로 잘리면 안 된다."""
    result = enforce("실패율이 높아요")

    assert result.hits == ("실패율",)
    assert result.text == "회복률이 높아요"
    assert "한 번 멈춤" not in result.text


def test_clean_text_is_untouched() -> None:
    """금지어가 없으면 원문 그대로 — 오탐이 톤을 망치지 않는다."""
    text = "오늘 GROUP BY 실습을 5분만 해볼까요?"
    result = enforce(text)

    assert result.text == text
    assert result.hits == ()
    assert result.changed is False
    assert result.blocked is False


def test_structured_walk_covers_nested_strings() -> None:
    """dict/list 중첩 안의 문자열도 전부 통과한다 — LLM 응답은 구조체다."""
    payload = {
        "if_clause": "책상에 앉으면",
        "then_clause": "실패한 부분부터 다시 해요",
        "tags": ["게으르다는 생각", "정상"],
        "meta": {"nested": {"deep": "왜 안 됐는지"}},
        "minutes": -30,  # 문자열 아닌 값은 그대로
    }
    sanitized, blocked, hits = enforce_structured(payload)

    assert "실패" not in sanitized["then_clause"]
    assert "게으르" not in sanitized["tags"][0]
    assert "왜 안" not in sanitized["meta"]["nested"]["deep"]
    assert sanitized["tags"][1] == "정상"
    assert sanitized["minutes"] == -30
    assert set(hits) >= {"실패", "게으르", "왜 안"}
    assert blocked is False


def test_enforce_many_processes_each_string() -> None:
    results = enforce_many(["실패했어요", "괜찮아요"])

    assert results[0].changed is True
    assert results[1].changed is False


# ── 현재 한계 고정 (개선하면 여기가 실패한다 — 의도된 것) ─────────────────


@pytest.mark.parametrize("inflected", ["게으른 하루였어요", "게으름 피웠네요"])
def test_known_limitation_inflected_forms_are_not_caught(inflected: str) -> None:
    """**한계**: 사전이 어간 문자열 매칭이라 활용형을 못 잡는다.

    사전 키가 '게으르'라서 '게으른'/'게으름'은 통과한다 — 비난 어휘가 그대로 나갈 수 있다.
    형태소 분석이나 어간 패턴(`게으[르른름]`)으로 개선하면 이 테스트가 실패하므로, 그때
    한계가 해소됐음을 명시적으로 갱신하면 된다.
    """
    assert scan(inflected) == (), "활용형이 잡히기 시작했다면 이 한계 테스트를 갱신할 것"


def test_known_limitation_substitution_can_produce_broken_grammar() -> None:
    """**한계**: 명사 치환이 활용 어미와 붙어 비문이 된다.

    '포기하고' → '잠깐 쉬어가는하고'. 톤 사고(비난 어휘 노출)는 막지만 문장이 어색해진다.
    blocked=False 라 그대로 사용자에게 나간다 — 치환어를 어미까지 고려해 고르거나
    문장 단위 재작성이 필요하다는 뜻이다.
    """
    result = enforce("포기하고 싶어질 때")

    assert result.text == "잠깐 쉬어가는하고 싶어질 때"
    assert result.blocked is False


def test_hard_block_set_is_empty_so_blocked_is_effectively_unreachable() -> None:
    """**한계**: `HARD_BLOCK_TERMS` 가 비어 있어 blocked 는 사실상 항상 False.

    즉 현재 필터는 **차단이 아니라 치환 전용**이다. tool_executor 의 `if blocked:`
    (reason='banned' fallback) 분기는 실질적으로 도달하지 않는다 — 문서가 말하는 '차단'
    시맨틱을 실제로 원한다면 HARD_BLOCK_TERMS 를 채워야 한다.
    """
    from reaction_backend.safety.banned_words import HARD_BLOCK_TERMS

    assert frozenset() == HARD_BLOCK_TERMS
    # 사전의 모든 금지어를 한 문장에 넣어도 blocked 는 False.
    everything = " ".join(BANNED_REPLACEMENTS)
    assert enforce(everything).blocked is False


# ── 실 경로 통합: fallback 도 필터를 거친다 ────────────────────────────


async def test_fallback_value_is_also_sanitized() -> None:
    """룰 fallback 문자열도 금지어 필터를 통과한다 (#20 DoD 8 구멍 봉합).

    회귀: `_fallback` 은 `_resolve_fallback` 결과를 **필터 없이** 반환했다. 대부분의
    fallback 은 신뢰된 카탈로그 템플릿이라 무해했지만, 사용자 입력을 되돌려주는 경로가
    있다 — inbox 의 `suggested_title=raw_text[:10]`. 사용자가 "실패한 프로젝트"를 캡처하면
    LLM 이 죽은 순간(키 없음·timeout) 그 문구가 필터를 우회해 응답에 실렸다.

    실제 `aiClient.run` 을 태운다(provider 미가용 → fallback 경로). 스텁이 아니다.
    """
    from pydantic import BaseModel

    from reaction_backend.llm import aiClient

    class _Echo(BaseModel):
        title: str

    result = await aiClient.run(
        module="inbox",
        schema=_Echo,
        prompt_id="inbox/classify",
        fallback=lambda: _Echo(title="실패한 프로젝트"),  # 사용자 입력 에코를 모사
        variables={"raw_text": "x"},
        timeout=0.01,  # provider 없음/즉시 실패 → fallback 분기
    )

    assert result.fell_back is True
    assert "실패" not in result.value.title, "fallback 이 금지어 필터를 우회한다"
    assert result.value.title == "한 번 멈춤한 프로젝트"


async def test_clean_fallback_is_returned_unchanged() -> None:
    """금지어 없는 fallback(대부분의 카탈로그 템플릿)은 그대로 — 불필요한 재검증 없음."""
    from pydantic import BaseModel

    from reaction_backend.llm import aiClient

    class _Echo(BaseModel):
        title: str

    result = await aiClient.run(
        module="inbox",
        schema=_Echo,
        prompt_id="inbox/classify",
        fallback=lambda: _Echo(title="오늘은 절반만, 가능한 만큼만 해볼까요?"),
        variables={"raw_text": "x"},
        timeout=0.01,
    )

    assert result.fell_back is True
    assert result.value.title == "오늘은 절반만, 가능한 만큼만 해볼까요?"
