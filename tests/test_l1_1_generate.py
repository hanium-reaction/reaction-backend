"""L1-1 생성 하네스 회귀 (`docs/experiments/preregistration-v1.md` §2).

실 LLM 호출은 monkeypatch 로 막는다 — 이 스위트는 변수 구성(`routes/recovery.py` 와
동일한지)과 호출 bookkeeping(case_id/version/repeat_index)만 고정한다.
"""

from __future__ import annotations

from typing import Any

import pytest
from scripts.l1_1_common import GenerationRow
from scripts.l1_1_generate import _generate_one, build_variables, run

from reaction_backend.llm import RunResult, aiClient
from reaction_backend.schemas.recovery import RecoveryProposalLLMv3
from tests.conftest import default_recovery_strategies


def _case(
    case_id: str,
    *,
    failure_tags: list[str],
    overwhelm_level: int = 0,
    title: str = "GROUP BY 실습",
    completion_status: str = "failed",
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "failure_tags": failure_tags,
        "context": {"overwhelm_level": overwhelm_level},
        "action_item": {"title": title},
        "execution": {"completion_status": completion_status},
    }


_VARIABLES_TEMPLATE = {
    "strategy_label": "5분 단위로 쪼개기",
    "strategy_group": "DOWNSCOPE",
    "base_template": "딱 5분만",
    "failure_type": "AMBIGUITY",
    "confidence": "n/a",
    "interruption_summary": "없음",
    "context_summary": "실행 카드: X / 결과: failed",
}


def test_build_variables_matches_route_construction() -> None:
    catalog = default_recovery_strategies()
    case = _case("single-AMBIGUITY-01", failure_tags=["AMBIGUITY"])

    variables = build_variables(case, catalog)

    assert variables["strategy_label"] == "5분 단위로 쪼개기"
    assert variables["strategy_group"] == "DOWNSCOPE"
    assert "GROUP BY 실습" in variables["base_template"]
    assert variables["failure_type"] == "AMBIGUITY"
    assert variables["confidence"] == "n/a"
    assert variables["interruption_summary"] == "없음"
    assert variables["context_summary"] == "실행 카드: GROUP BY 실습 / 결과: failed"


def test_build_variables_joins_multiple_failure_tags() -> None:
    catalog = default_recovery_strategies()
    case = _case("multi-FATIGUE-LOW_ENERGY-01", failure_tags=["FATIGUE", "LOW_ENERGY"])

    variables = build_variables(case, catalog)

    assert variables["failure_type"] == "FATIGUE, LOW_ENERGY"


def test_build_variables_overwhelm_level_triggers_park_default_when_no_tag_matches() -> None:
    """태그가 하나도 안 걸려도 overwhelm>=4 면 PARK_DEFAULT 가 선두가 된다.

    골든셋 `boundary` 블록이 정확히 이 동적 트리거를 시험하려고 만들어졌다
    (`orchestrator/recovery.py::OVERWHELM_PARK_THRESHOLD`).
    """
    catalog = default_recovery_strategies()
    case = _case("boundary-overwhelm-4", failure_tags=[], overwhelm_level=4)

    variables = build_variables(case, catalog)

    assert variables["strategy_group"] == "PARK"


def test_build_variables_raises_on_empty_selection() -> None:
    """카탈로그가 비어 있으면(또는 전부 비활성) `select_strategies` 가 빈 리스트를 준다.

    조용히 넘어가면 이후 단계가 `IndexError` 로 원인 불명확하게 죽는다 — 여기서 먼저
    잡아 원인을 case_id 와 함께 보여준다.
    """
    case = _case("no-catalog", failure_tags=["AMBIGUITY"])

    with pytest.raises(ValueError, match="no-catalog"):
        build_variables(case, [])


def _make_stub_run(*, fallback_on: set[tuple[str, int]] | None = None) -> Any:
    """`aiClient.run` 대체 — 호출마다 버전별로 다른 값을 돌려주고 호출 횟수를 센다."""
    fallback_on = fallback_on or set()
    call_count = {"n": 0}

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        call_count["n"] += 1
        prompt_id = kwargs["prompt_id"]
        version = prompt_id.rsplit("@v", 1)[1]
        variables = kwargs["variables"]
        schema_cls = kwargs["schema"]

        if (version, call_count["n"]) in fallback_on:
            return RunResult(
                value=kwargs["fallback"](),
                fell_back=True,
                reason="timeout",
                prompt_id="recovery/if_then_proposal",
                prompt_version=version,
            )

        # schema_cls 가 실제로 넘어온 것으로 만든다 — l1_1_generate.py 가 v1/v2 에
        # RecoveryProposalLLMv3 를 잘못 넘기면(회귀) 여기서 obstacle= 등 미지원 kwarg 로
        # TypeError 가 나서 바로 잡힌다.
        extra = {"obstacle": "obstacle", "coping_clause": "coping"} if version == "3" else {}
        return RunResult(
            value=schema_cls(
                strategy_code=variables["strategy_group"].lower(),
                if_clause=f"if-v{version}",
                then_clause=f"then-v{version}",
                rationale="rationale",
                **extra,
            ),
            fell_back=False,
            reason=None,
            prompt_id="recovery/if_then_proposal",
            prompt_version=version,
        )

    return stub_run


@pytest.mark.asyncio
async def test_run_produces_one_row_per_case_version_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aiClient, "run", _make_stub_run())

    catalog = default_recovery_strategies()
    cases = [
        _case("c1", failure_tags=["AMBIGUITY"]),
        _case("c2", failure_tags=["CONFLICT"]),
    ]

    rows = await run(cases, catalog, versions=("1", "2"), repeats=2)

    assert len(rows) == 2 * 2 * 2  # 2 cases × 2 versions × 2 repeats
    # bookkeeping — 모든 (case_id, version, repeat_index) 조합이 정확히 한 번씩.
    keys = {(r.case_id, r.version, r.repeat_index) for r in rows}
    assert keys == {
        (case["case_id"], version, rep)
        for case in cases
        for version in ("1", "2")
        for rep in (0, 1)
    }


@pytest.mark.asyncio
async def test_run_carries_context_and_llm_fields_into_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aiClient, "run", _make_stub_run())

    catalog = default_recovery_strategies()
    cases = [_case("c1", failure_tags=["AMBIGUITY"])]

    rows = await run(cases, catalog, versions=("3",), repeats=1)

    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, GenerationRow)
    assert row.if_clause == "if-v3"
    assert row.obstacle == "obstacle"
    assert row.coping_clause == "coping"
    assert row.fell_back is False
    assert row.failure_type == "AMBIGUITY"
    assert row.strategy_group == "DOWNSCOPE"


@pytest.mark.asyncio
async def test_run_records_fallback_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    """fallback 이 나면 fell_back=True/reason 이 그대로 기록된다 — 숨기지 않는다."""
    monkeypatch.setattr(aiClient, "run", _make_stub_run(fallback_on={("1", 1)}))

    catalog = default_recovery_strategies()
    cases = [_case("c1", failure_tags=["AMBIGUITY"])]

    rows = await run(cases, catalog, versions=("1",), repeats=1)

    assert len(rows) == 1
    assert rows[0].fell_back is True
    assert rows[0].reason == "timeout"


class TestAcknowledgmentGateEnforcement:
    """v3 실 dispatch 실측(gemini-3.5-flash-lite) — 조건부 지시("AVOIDANCE 아니면 빈
    문자열")를 모델이 신뢰성 있게 안 지켰다(5건 중 4건 위반). banned_words 와 같은 패턴으로
    코드가 마지막에 강제한다 — `_generate_one` 이 그 강제를 실제로 하는지 고정한다.
    """

    @pytest.mark.asyncio
    async def test_strips_acknowledgment_when_failure_type_is_not_avoidance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def stub_run(**kwargs: Any) -> RunResult[Any]:
            return RunResult(
                value=RecoveryProposalLLMv3(
                    strategy_code="reschedule",
                    if_clause="if",
                    then_clause="then",
                    rationale="rationale",
                    acknowledgment="지시를 어기고 채워진 위로 문장",
                ),
                fell_back=False,
                reason=None,
                prompt_id="recovery/if_then_proposal",
                prompt_version="3",
            )

        monkeypatch.setattr(aiClient, "run", stub_run)

        row = await _generate_one(
            "c1", "3", 0, {**_VARIABLES_TEMPLATE, "failure_type": "TIME_SHORTAGE"}
        )

        assert row.acknowledgment == ""

    @pytest.mark.asyncio
    async def test_keeps_acknowledgment_when_failure_type_is_avoidance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def stub_run(**kwargs: Any) -> RunResult[Any]:
            return RunResult(
                value=RecoveryProposalLLMv3(
                    strategy_code="downscope",
                    if_clause="if",
                    then_clause="then",
                    rationale="rationale",
                    acknowledgment="누구나 시작이 막막할 때가 있어요",
                ),
                fell_back=False,
                reason=None,
                prompt_id="recovery/if_then_proposal",
                prompt_version="3",
            )

        monkeypatch.setattr(aiClient, "run", stub_run)

        row = await _generate_one(
            "c1", "3", 0, {**_VARIABLES_TEMPLATE, "failure_type": "AVOIDANCE"}
        )

        assert row.acknowledgment == "누구나 시작이 막막할 때가 있어요"
