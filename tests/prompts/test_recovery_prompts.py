"""Recovery 프롬프트 렌더 회귀 (AGENTS.md §6).

`recovery/if_then_proposal` 이 personalize 하려면 route 가 넘기는 변수(선두 전략 정보 포함)가
템플릿 `{{var}}` 와 정확히 일치해야 한다. 누락되면 PromptRenderError → 조용히 룰 fallback
(카탈로그 템플릿)으로 빠지므로 — 그 은폐를 여기서 잡는다.

⚠️ 검사 대상을 **파일명으로 고정하지 않는다**. registry 는 버전을 생략한 `prompt_id` 를
`latest()`(SemVer-ish 최댓값)로 해석하므로, 새 버전 파일을 디렉터리에 **떨어뜨리기만 해도
프로덕션이 그 버전으로 갈아탄다**. 예전엔 이 파일이 `if_then_proposal.v1.md` 를 하드코딩해서,
계약이 다른 v2 를 넣어도 테스트는 v1 만 보고 통과했다(#57 의 orphan v2 가 정확히 그 경우 —
`strategy_label`/`strategy_group`/`base_template` 없이 LLM 이 직접 전략을 고르게 하는 옛 설계라,
룰이 고른 전략과 다른 문구가 그 전략 라벨에 찍히는 모순이 조용히 프로덕션에 나간다).
그래서 **registry 가 실제로 해석하는 것**과 **존재하는 모든 버전**을 함께 검사한다.

⚠️ 위 검사만으로는 부족하다 — "구조적으로 호환되는가"와 "지금 프로덕션에 태울 만한가"는
다른 질문이다(문구 품질·톤은 L1-1 오프라인 A/B 로 검증되지 전까지 알 수 없다). 그래서
`api/routes/recovery.py::_PROMPT_ID_V2`/`_PROMPT_ID_V3` 가 **명시 버전**(`@vN`)으로
프로덕션이 실제로 쓰는 두 버전을 못박는다 — registry 의 "버전 생략 시 latest" 규칙과
별개로, 새 버전이 디렉터리에 있어도 이 상수들을 올리기 전까진 프로덕션은 그대로다. 그
고정이 유지되는지, 그리고 v3 가 AVOIDANCE 태그로만 좁혀 라우팅되는지도 여기서 같이 본다.
"""

from __future__ import annotations

import re

import pytest

from reaction_backend.api.routes.recovery import (
    _PROMPT_ID_V2,
    _PROMPT_ID_V3,
    _V3_TRIGGER_TAG,
)
from reaction_backend.prompts import registry
from reaction_backend.prompts.registry import PromptRenderError, PromptTemplate

# route(api/routes/recovery.py) 의 generate 가 넘기는 변수 집합과 동기화.
CODE_VARS: dict[str, set[str]] = {
    "recovery/if_then_proposal": {
        "strategy_label",
        "strategy_group",
        "base_template",
        "failure_type",
        "confidence",
        "interruption_summary",
        "context_summary",
    },
}
_PH = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _placeholders(body: str) -> set[str]:
    return set(_PH.findall(body))


def _all_versions(prompt_id: str) -> list[PromptTemplate]:
    domain, name = prompt_id.split("/", 1)
    return [t for t in registry.list_all() if t.domain == domain and t.name == name]


@pytest.mark.parametrize("prompt_id", list(CODE_VARS))
def test_resolved_prompt_placeholders_match_code_variables(prompt_id: str) -> None:
    """**registry 가 해석한 것**(= 프로덕션이 쓰는 것)이 코드의 변수 계약과 일치한다."""
    resolved = registry.get(prompt_id)
    assert _placeholders(resolved.body) == CODE_VARS[prompt_id], (
        f"{resolved.full_id} 의 placeholder 가 route 가 넘기는 변수와 다르다. "
        "버전을 새로 올렸다면 코드 계약(strategy_label/strategy_group/base_template 포함)을 지킬 것."
    )


@pytest.mark.parametrize("prompt_id", list(CODE_VARS))
def test_every_version_matches_code_variables(prompt_id: str) -> None:
    """존재하는 **모든** 버전이 계약을 지킨다 — 파일을 넣는 순간 latest 가 될 수 있으므로.

    계약이 다른 초안을 디렉터리에 두는 것 자체가 프로덕션 전환 트리거다(별도 승격 절차 없음).
    """
    versions = _all_versions(prompt_id)
    assert versions, f"{prompt_id} 템플릿이 하나도 없다"
    for tmpl in versions:
        assert _placeholders(tmpl.body) == CODE_VARS[prompt_id], (
            f"{tmpl.full_id} 가 계약을 어긴다 — 이 파일이 존재하는 것만으로 latest 가 되어 "
            "프로덕션에 나갈 수 있다."
        )


@pytest.mark.parametrize("prompt_id", list(CODE_VARS))
def test_renders_without_missing_variables(prompt_id: str) -> None:
    text, _ = registry.render(prompt_id, dict.fromkeys(CODE_VARS[prompt_id], "x"))
    assert text.strip()
    assert "{{" not in text


def test_resolved_prompt_keeps_strategy_personalization_contract() -> None:
    """해석된 프롬프트가 '룰이 고른 전략을 personalize' 계약을 유지한다.

    route 는 LLM 이 돌려준 `strategy_code` 를 **검사하지 않고** 문구를 룰이 고른 전략
    (`top.strategy_type`)에 그대로 붙인다. 그래서 프롬프트가 LLM 에게 전략을 직접 고르라고
    시키면, 고른 전략의 문구가 **다른 전략 라벨에 찍히는** 모순이 조용히 나간다.
    선두 전략을 알려주는 변수 3종이 프롬프트에 살아 있어야 그 사고가 구조적으로 막힌다.
    """
    body = registry.get("recovery/if_then_proposal").body
    for var in ("strategy_label", "strategy_group", "base_template"):
        assert f"{{{{{var}}}}}" in body, (
            f"{var} 가 프롬프트에서 사라졌다 — LLM 이 룰의 선택을 모른 채 제 전략을 고르게 된다."
        )


def test_missing_variable_raises() -> None:
    with pytest.raises(PromptRenderError):
        registry.render("recovery/if_then_proposal", {})


@pytest.mark.parametrize("prompt_id", list(CODE_VARS))
def test_few_shot_examples_are_banned_word_free(prompt_id: str) -> None:
    """모든 버전의 few-shot **예시 출력값**에 금지어가 없다.

    LLM 은 예시를 모방한다 — 예시에 "실패" 같은 금지어가 들어가면 출력 히트 빈도가 올라가고,
    런타임 필터는 차단이 아니라 **치환**이라("실패"→"한 번 멈춤") 어색한 비문이 회복 카드에
    노출된다. 지시문의 금지어 *인용*("~표현 금지")은 의도된 것이므로, JSON 으로 파싱되는
    예시 블록의 문자열 값만 검사한다.
    """
    import json

    from reaction_backend.safety.banned_words import scan

    for tmpl in _all_versions(prompt_id):
        # 본문에서 { ... } 블록을 추출해 JSON 으로 파싱되는 것만 = few-shot 예시.
        # (출력 형식 블록은 <placeholder> 가 unquoted 라 파싱에 실패해 자연히 제외된다.)
        examples: list[dict[str, object]] = []
        for match in re.finditer(r"\{[^{}]*\}", tmpl.body, re.DOTALL):
            try:
                examples.append(json.loads(match.group(0)))
            except (json.JSONDecodeError, ValueError):
                continue
        if "예시" in tmpl.body:
            assert examples, (
                f"{tmpl.full_id}: 예시 섹션이 있는데 파싱된 예시 0개 — 검사가 공허해진다"
            )
        for example in examples:
            for key, value in example.items():
                if not isinstance(value, str):
                    continue
                hits = scan(value)
                assert not hits, (
                    f"{tmpl.full_id} 예시의 {key} 에 금지어 {hits} — LLM 이 모방해 출력하면 "
                    "치환 비문이 사용자 카드에 노출된다."
                )


# ── 프로덕션 버전 고정 — 새 파일을 떨어뜨리는 것만으로 프로덕션이 갈아타지 않는다 ──


@pytest.mark.parametrize("prompt_id", [_PROMPT_ID_V2, _PROMPT_ID_V3])
def test_production_prompt_id_pins_an_explicit_version(prompt_id: str) -> None:
    """`_PROMPT_ID_V2`/`_PROMPT_ID_V3` 가 각각 `@vN` 을 명시한다.

    둘 중 하나라도 버전 없는 `"recovery/if_then_proposal"` 로 되돌아가면, 디렉터리에
    더 새 버전 파일을 추가하는 순간 승격 절차 없이 프로덕션이 자동으로 갈아탄다.
    """
    assert "@v" in prompt_id, (
        f"{prompt_id!r} 에 버전이 없다 — registry 가 latest 로 해석해 새 버전 파일을 "
        "디렉터리에 두기만 해도 프로덕션이 자동 전환된다."
    )


@pytest.mark.parametrize("prompt_id", [_PROMPT_ID_V2, _PROMPT_ID_V3])
def test_production_prompt_id_resolves(prompt_id: str) -> None:
    """고정된 두 버전이 실제로 존재한다 — 파일을 지우거나 이름을 바꾸면 여기서 먼저 죽는다."""
    resolved = registry.get(prompt_id)
    assert resolved.full_id == prompt_id
    assert resolved.body.strip()


@pytest.mark.parametrize("prompt_id", [_PROMPT_ID_V2, _PROMPT_ID_V3])
def test_production_prompt_id_matches_code_variables(prompt_id: str) -> None:
    """고정된 두 버전이 route 의 변수 계약을 지킨다 — latest 가 앞서가도 이 버전들은 안 흔들린다."""
    resolved = registry.get(prompt_id)
    assert _placeholders(resolved.body) == CODE_VARS["recovery/if_then_proposal"], (
        f"{resolved.full_id}(프로덕션 고정 버전) 의 placeholder 가 route 변수와 다르다."
    )


# ── v3 (AVOIDANCE 전용 조건부 라우팅 — acknowledgment/v3 승격, 근거 대장 §4 S1/S5) ──


def test_v3_output_schema_includes_coping_and_acknowledgment_fields() -> None:
    """v3 출력 형식 블록에 obstacle/coping_clause/acknowledgment 3필드가 있다.

    이 필드들은 `RecoveryProposalLLM` 스키마에도 기본값(빈 문자열)으로 있어 v1/v2 의
    fallback 구성(이 필드들을 안 채움, `tests/test_recovery.py` 의 스텁 참고)이 계속
    검증을 통과한다.
    """
    body = registry.get("recovery/if_then_proposal@v3").body
    for field in ("obstacle", "coping_clause", "acknowledgment"):
        assert f'"{field}"' in body, f"v3 출력 형식 블록에 {field} 가 없다"


def test_v3_acknowledgment_is_gated_on_avoidance_tag() -> None:
    """acknowledgment 는 AVOIDANCE 태그 조건부다(근거 대장 §4 S1) — 매번 위로하지 않는다.

    overwhelm≥4 / 연속실패≥2 조건은 아직 실 데이터가 없어(도그푸딩 #258 미착수,
    근거 대장 §5.4 "로그에서 추정할 것") v3 프롬프트에 포함하지 않았다 — AVOIDANCE
    태그 조건 하나만 지금 있는 변수(failure_type)로 구현 가능한 부분 구현이다.
    """
    body = registry.get("recovery/if_then_proposal@v3").body
    assert "AVOIDANCE" in body
    assert "빈 문자열" in body


def test_v3_is_pinned_alongside_v2_not_instead_of_it() -> None:
    """v3 승격은 **AVOIDANCE 태그 조건부**다 — v2 고정을 지우고 갈아탄 게 아니다.

    L1-1 오프라인 평가(승률 1.000)가 근거지만 judge–human κ=0.482 로 보조 지표
    강등(#278)됐고 실 도그푸딩 검증도 없다 — 그래서 무조건 승격 대신 노출 범위를
    AVOIDANCE 태그로 좁혔다(발견 ②의 재발 방지 정신은 유지: 두 상수 다 명시 버전
    고정이라 latest 자동 승격에 노출되지 않는다).
    """
    assert _PROMPT_ID_V2 == "recovery/if_then_proposal@v2"
    assert _PROMPT_ID_V3 == "recovery/if_then_proposal@v3"
    assert registry.get(_PROMPT_ID_V2).version == "2"
    assert registry.get(_PROMPT_ID_V3).version == "3"
    assert _V3_TRIGGER_TAG == "AVOIDANCE"
