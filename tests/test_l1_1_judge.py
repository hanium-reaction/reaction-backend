"""L1-1 판정 하네스 회귀 — 페어링/블라인딩/재시도 로직. 실 LLM 호출은 monkeypatch."""

from __future__ import annotations

from typing import Any

import pytest
from scripts.l1_1_common import GenerationRow, JudgeVerdict
from scripts.l1_1_judge import (
    build_judge_prompt,
    judge_unit,
    run,
    select_judgment_units,
)

from reaction_backend.llm.provider import ProviderUnavailable


def _row(
    case_id: str, version: str, repeat_index: int, *, fell_back: bool = False
) -> GenerationRow:
    return GenerationRow(
        case_id=case_id,
        version=version,
        repeat_index=repeat_index,
        fell_back=fell_back,
        reason="timeout" if fell_back else None,
        strategy_code="downscope",
        if_clause=f"if-v{version}-r{repeat_index}",
        then_clause=f"then-v{version}-r{repeat_index}",
        rationale="rationale",
        obstacle="obstacle" if version == "3" else "",
        coping_clause="coping" if version == "3" else "",
        acknowledgment="",
        estimated_workload_change_minutes=0,
        failure_type="AMBIGUITY",
        strategy_label="5분 단위로 쪼개기",
        strategy_group="DOWNSCOPE",
        base_template="딱 5분만",
        context_summary="실행 카드: X / 결과: failed",
    )


class TestSelectJudgmentUnits:
    def test_picks_first_two_common_reps_per_pair(self) -> None:
        rows = [_row("c1", v, r) for v in ("1", "2", "3") for r in range(3)]

        units, shortfalls = select_judgment_units(rows)

        assert not shortfalls
        # 3쌍 × 2반복 = 6 유닛.
        assert len(units) == 6
        reps_by_pair = {
            u.pair: sorted(x.rep_index for x in units if x.pair == u.pair) for u in units
        }
        assert reps_by_pair["1-2"] == [0, 1]
        assert reps_by_pair["2-3"] == [0, 1]
        assert reps_by_pair["1-3"] == [0, 1]

    def test_skips_fallback_reps_and_picks_next_available(self) -> None:
        rows = [
            _row("c1", "1", 0, fell_back=True),  # v1 rep0 은 못 씀
            _row("c1", "1", 1),
            _row("c1", "1", 2),
            _row("c1", "2", 0),
            _row("c1", "2", 1),
            _row("c1", "2", 2),
        ]

        units, shortfalls = select_judgment_units(rows, pairs=(("1", "2"),))

        assert not shortfalls
        reps = sorted(u.rep_index for u in units)
        assert reps == [1, 2]  # rep0 은 v1 이 fallback 이라 건너뛴다

    def test_records_shortfall_when_fewer_than_two_common_reps(self) -> None:
        rows = [
            _row("c1", "1", 0),
            _row("c1", "1", 1, fell_back=True),
            _row("c1", "1", 2, fell_back=True),
            _row("c1", "2", 0),
            _row("c1", "2", 1),
            _row("c1", "2", 2),
        ]

        units, shortfalls = select_judgment_units(rows, pairs=(("1", "2"),))

        assert len(units) == 1  # rep0 하나만 공통으로 성공
        assert shortfalls == {"c1:1-2": 1}


class TestJudgeUnit:
    @pytest.mark.asyncio
    async def test_forward_and_reversed_presentations_map_versions_correctly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen_prompts: list[str] = []

        async def fake_call_judge(prompt: str, **kwargs: Any) -> JudgeVerdict:
            seen_prompts.append(prompt)
            return JudgeVerdict.model_validate(
                {
                    "candidate_a": {"axis1": 3, "axis2": 3, "axis3": 3, "axis4": 3, "axis5": 3},
                    "candidate_b": {"axis1": 4, "axis2": 4, "axis3": 4, "axis4": 4, "axis5": 4},
                    "axis4_disqualification_reason": None,
                }
            )

        monkeypatch.setattr("scripts.l1_1_judge._call_judge", fake_call_judge)

        units, _ = select_judgment_units(
            [_row("c1", "1", 0), _row("c1", "3", 0)], pairs=(("1", "3"),)
        )
        unit = units[0]

        rows = await judge_unit(unit, forward_a_is_low=True, model="m", timeout=1.0, max_attempts=1)

        assert len(rows) == 2
        forward = next(r for r in rows if not r.swap)
        reversed_ = next(r for r in rows if r.swap)
        # 정방향: A=낮은 버전(v1), 역방향: A=높은 버전(v3) — 서로 뒤집혀 있어야 함.
        assert forward.version_a == "1" and forward.version_b == "3"
        assert reversed_.version_a == "3" and reversed_.version_b == "1"
        # 후보 JSON 에 버전 식별 정보가 절대 안 새어 나가야 한다 (루브릭 §4-1 블라인딩).
        for prompt in seen_prompts:
            assert "version" not in prompt.lower()

    @pytest.mark.asyncio
    async def test_dropped_direction_is_excluded_not_fabricated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """심판 호출이 끝까지 실패하면 그 방향은 결과에서 아예 빠진다(가짜 점수로 안 채움)."""
        call_count = {"n": 0}

        async def flaky_call_judge(prompt: str, **kwargs: Any) -> JudgeVerdict | None:  # noqa: ARG001
            call_count["n"] += 1
            if call_count["n"] == 1:
                return JudgeVerdict.model_validate(
                    {
                        "candidate_a": {"axis1": 3, "axis2": 3, "axis3": 3, "axis4": 3, "axis5": 3},
                        "candidate_b": {"axis1": 3, "axis2": 3, "axis3": 3, "axis4": 3, "axis5": 3},
                        "axis4_disqualification_reason": None,
                    }
                )
            return None  # 두 번째 호출(역방향)은 재시도 끝에 실패

        monkeypatch.setattr("scripts.l1_1_judge._call_judge", flaky_call_judge)

        units, _ = select_judgment_units(
            [_row("c1", "1", 0), _row("c1", "3", 0)], pairs=(("1", "3"),)
        )
        rows = await judge_unit(
            units[0], forward_a_is_low=True, model="m", timeout=1.0, max_attempts=1
        )

        assert len(rows) == 1
        assert not rows[0].swap


class TestBuildJudgePrompt:
    def test_includes_all_context_and_candidates(self) -> None:
        prompt = build_judge_prompt(
            failure_type="AMBIGUITY",
            strategy_label="5분 단위로 쪼개기",
            strategy_group="DOWNSCOPE",
            base_template="딱 5분만",
            context_summary="실행 카드: X / 결과: failed",
            candidate_a_json='{"if_clause": "A"}',
            candidate_b_json='{"if_clause": "B"}',
        )

        assert "AMBIGUITY" in prompt
        assert '{"if_clause": "A"}' in prompt
        assert '{"if_clause": "B"}' in prompt
        assert "axis4_disqualification_reason" in prompt


@pytest.mark.asyncio
async def test_run_is_deterministic_given_same_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_call_judge(prompt: str, **kwargs: Any) -> JudgeVerdict:  # noqa: ARG001
        return JudgeVerdict.model_validate(
            {
                "candidate_a": {"axis1": 3, "axis2": 3, "axis3": 3, "axis4": 3, "axis5": 3},
                "candidate_b": {"axis1": 3, "axis2": 3, "axis3": 3, "axis4": 3, "axis5": 3},
                "axis4_disqualification_reason": None,
            }
        )

    monkeypatch.setattr("scripts.l1_1_judge._call_judge", fake_call_judge)

    rows = [_row("c1", v, r) for v in ("1", "2", "3") for r in range(3)]
    units, _ = select_judgment_units(rows)

    result_1 = await run(units, seed=42)
    result_2 = await run(units, seed=42)

    assert [r.version_a for r in result_1] == [r.version_a for r in result_2]
    assert len(result_1) == len(units) * 2


@pytest.mark.asyncio
async def test_provider_unavailable_stops_retrying_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = {"n": 0}

    async def unavailable(*args: Any, **kwargs: Any) -> Any:
        call_count["n"] += 1
        raise ProviderUnavailable("no api key")

    monkeypatch.setattr("scripts.l1_1_judge.generate_structured", unavailable)

    from scripts.l1_1_judge import _call_judge

    result = await _call_judge("prompt", model="m", timeout=1.0, max_attempts=3)

    assert result is None
    assert call_count["n"] == 1  # 재시도 없이 바로 포기 (키 없음은 재시도해도 안 됨)
