"""엔드포인트 호출 상한의 **계수 단위** (#370).

`test_endpoint_rate_limit.py` 는 가드를 합성 `llm_runs` 행으로만 검증했다. 그래서 가드는
자기 가정("행 1개 = 실행 1회")에 대해서만 초록불이었고, 실제 호출자가 한 번에 2~3행을
남긴다는 사실을 아무도 못 봤다 — 배포된 서비스에서 **인터뷰를 아무도 완주할 수 없었다.**

이 파일은 가드를 **실제 호출자에 붙여서** 못 박는다:

1. 계획 인터뷰 완주에 필요한 LLM 호출 수 > 상한 (예전 단위로는 반드시 막힌다)
2. 그런데 완주에 필요한 **요청 수** < 상한 (지금 단위로는 통과한다)
3. 한 요청 안의 모든 LLM 호출이 같은 trace_id 를 단다 (미들웨어 → contextvar → tool_executor)
4. 클라이언트가 보낸 `X-Request-ID` 로 그 값을 조종할 수 없다 (상한 우회 차단)
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from reaction_backend.config import get_settings
from reaction_backend.llm import RunResult, aiClient, tool_executor
from reaction_backend.llm.provider import ProviderUnavailable
from reaction_backend.observability.correlation import (
    CorrelationMiddleware,
    get_trace_id,
)
from reaction_backend.orchestrator import interview_catalog, interview_runner
from reaction_backend.schemas.interview import (
    AmbiguityUpdate,
    AnswerIntake,
    InterviewSummary,
    NextQuestionSchema,
)

# ── 인터뷰 완주 드라이버 (tests/test_interview_runner.py 의 stub 패턴) ────────

_RANGE_SLOTS = {"time.activity_window"}
_CHIP_SLOTS = {
    "identity.role",
    "identity.season",
    "time.peak_window",
    "recovery.tone",
    "recovery.rest_ok",
    "recovery.downscope_unit",
}


def _answer_for(slot_key: str) -> Any:
    if slot_key in _RANGE_SLOTS:
        return {"start": "09:00", "end": "23:00"}
    if slot_key in _CHIP_SLOTS:
        if slot_key == "time.peak_window":
            return ["오전"]
        if slot_key == "recovery.downscope_unit":
            return ["10분"]
        return ["네"]
    if slot_key == "goals.list":
        return "캡스톤, 토익"
    if slot_key == "goals.heaviest":
        return "캡스톤"
    if slot_key == "goals.deadlines":
        return "2026-06-20"
    return "이번 학기 안에 캡스톤 프로젝트를 끝내는 게 제일 급해요"


def _counting_stub(calls: list[str]):
    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        calls.append(kwargs["prompt_id"])
        if schema is NextQuestionSchema:
            value: Any = NextQuestionSchema(question="다음 질문", empathy_one_liner="좋아요")
        elif schema in (AmbiguityUpdate, AnswerIntake):
            # `AnswerIntake` 는 `AmbiguityUpdate` 의 상위집합이다(+ slots). 채점 전용 호출과
            # 수확이 합쳐진 호출이 **같은 스키마**를 쓰므로 여기서 갈리지 않는다.
            value = schema(
                slot_key=kwargs["variables"]["slot_key"],
                clarity_score=0.9,
                new_ambiguity=0.1,
            )
        elif schema is InterviewSummary:
            value = InterviewSummary(
                headline="요약",
                goal_summary="목표",
                time_summary="시간",
                preference_summary="선호",
                confirm_question="이대로 계획을 세워볼까요?",
            )
        else:  # pragma: no cover
            raise AssertionError(f"unexpected schema {schema}")
        return RunResult(
            value=value,
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    return stub_run


async def _drive_full_plan_interview(monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
    """계획 인터뷰를 끝까지 진행하고 (요청 수, LLM 호출 수) 를 돌려준다.

    요청 수 = 세션 시작 1 + 답변 제출 턴 수. 라우터가 가드를 거는 단위가 이것이다.
    """
    calls: list[str] = []
    monkeypatch.setattr(aiClient, "run", _counting_stub(calls))

    result = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    requests = 1  # POST /interview/sessions

    guard = 0
    while not result.done and guard < 40:
        slot = result.state["next_slot_key"]
        assert slot is not None
        result = await interview_runner.submit_and_advance(
            state=result.state, slot_key=slot, answer_value=_answer_for(slot)
        )
        requests += 1  # POST /interview/sessions/{id}/answers
        guard += 1

    assert result.done is True, "인터뷰가 완주하지 않았다 — 드라이버가 깨졌다"
    assert result.end_reason == "completed"
    return requests, len(calls)


async def test_counting_rows_would_still_block_the_interview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#370 재현 — 계수 단위를 행으로 되돌리면 인터뷰가 다시 막힌다.

    비교 대상은 **base 상한**(planning·recovery 가 지금도 쓰는 값, 그리고 이 PR 이전에
    인터뷰도 쓰던 값)이다. 완주 LLM 호출 수가 그 위에 있는 한, 행을 세는 구현으로
    되돌아가면 온보딩은 즉시 다시 깨진다.

    두 단언(이것과 아래 '요청 수')이 함께 있어야 의미가 있다 — 하나는 왜 단위를 바꿨는지,
    다른 하나는 바꾼 단위가 실제로 통하는지를 지킨다.
    """
    base_limit = get_settings().llm_endpoint_daily_call_limit
    if base_limit <= 0:  # 무제한 설정 — 이 단언의 의미가 없다
        pytest.skip("LLM_ENDPOINT_DAILY_CALL_LIMIT=0 (무제한)")

    requests, llm_calls = await _drive_full_plan_interview(monkeypatch)

    assert llm_calls > base_limit, (
        f"완주에 LLM {llm_calls}콜이 드는데 base 상한이 {base_limit} 이다. "
        "행 수로 세면 인터뷰가 중간에 429 로 막힌다 — 이게 #370 이었다."
    )
    assert llm_calls > requests, (
        f"요청 {requests}건에 LLM {llm_calls}콜 — 단위를 잘못 고르면 상한이 "
        f"{llm_calls / requests:.1f}배로 조여진다."
    )


async def test_full_interview_fits_within_the_daily_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """진짜 계약 — 신규 사용자가 **하루 안에** 계획 인터뷰를 끝낼 수 있어야 한다.

    가드가 요청을 세는 한, 완주에 필요한 요청 수(세션 시작 + 필수 슬롯 턴)가 상한 아래면
    온보딩이 막히지 않는다. 슬롯을 크게 늘리거나 상한을 낮추면 여기서 먼저 걸린다.
    """
    limit = get_settings().endpoint_call_limit_for_module("interview")
    if limit <= 0:
        pytest.skip("interview 상한 0 (무제한)")

    requests, _ = await _drive_full_plan_interview(monkeypatch)

    required_slots = len(interview_catalog.PLAN_CATALOG.required_keys)
    assert requests <= limit, (
        f"계획 인터뷰 완주에 요청 {requests}건(필수 슬롯 {required_slots}개)이 필요한데 "
        f"상한이 {limit} 이다. 신규 사용자가 온보딩을 끝낼 수 없다 (#370)."
    )


# ── trace_id 배선: 한 요청 = 한 trace_id ────────────────────────────────


class _Schema(BaseModel):
    pass


class _Tmpl:
    prompt_id = "test/trace"
    version = "v1"


@pytest.fixture
def _fake_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """프롬프트 렌더는 고정, provider 는 항상 실패 → 폴백 경로. 폴백도 llm_runs 를 남긴다."""
    monkeypatch.setattr(
        tool_executor.prompt_registry, "render", lambda pid, variables: ("본문", _Tmpl())
    )

    async def _unavailable(**kwargs: Any) -> Any:
        raise ProviderUnavailable("no key (test)")

    monkeypatch.setattr(tool_executor, "generate_structured", _unavailable)


async def test_calls_in_one_request_share_a_trace_id(_fake_llm: None) -> None:
    """미들웨어가 심은 trace_id 를 `aiClient.run` 이 인자 없이도 집어 든다.

    이게 성립해야 `endpoint_rate_limit` 의 `DISTINCT trace_id` 가 '실행 횟수'가 된다.
    호출부를 하나하나 고치지 않고 contextvar 로 덮은 이유이기도 하다.
    """
    seen: list[str | None] = []

    app = FastAPI()

    @app.get("/two-calls")
    async def two_calls() -> dict[str, str | None]:
        # 요청 안에서 LLM 을 두 번 부른다 — 인터뷰 한 턴이 하는 일과 같은 모양.
        for _ in range(2):
            await aiClient.run(
                module="interview",
                schema=_Schema,
                prompt_id="test/trace",
                fallback=_Schema(),
            )
            seen.append(get_trace_id())
        return {"trace": get_trace_id()}

    app.add_middleware(CorrelationMiddleware)

    with TestClient(app) as client:
        first = client.get("/two-calls")
        second = client.get("/two-calls")

    assert first.status_code == 200
    # 한 요청 안 두 호출은 같은 값
    assert seen[0] is not None and seen[0] == seen[1]
    assert seen[2] is not None and seen[2] == seen[3]
    # 서로 다른 요청은 다른 값 — 아니면 상한이 영원히 1 로 고정된다
    assert seen[0] != seen[2]
    assert first.headers["x-request-id"] == first.json()["trace"]
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


def test_client_cannot_choose_the_trace_id() -> None:
    """클라이언트가 보낸 `X-Request-ID` 를 신뢰하면 상한을 통째로 우회할 수 있다.

    매 요청에 같은 값을 보내면 하루치 호출이 `DISTINCT` 1건으로 접힌다. trace_id 가
    가드 입력이 된 이상 서버 생성 값만 쓴다.
    """
    app = FastAPI()

    @app.get("/echo")
    async def echo() -> dict[str, str | None]:
        return {"trace": get_trace_id()}

    app.add_middleware(CorrelationMiddleware)

    forged = "attacker-fixed-value"
    with TestClient(app) as client:
        a = client.get("/echo", headers={"X-Request-ID": forged})
        b = client.get("/echo", headers={"X-Request-ID": forged})

    assert a.json()["trace"] != forged
    assert b.json()["trace"] != forged
    assert a.json()["trace"] != b.json()["trace"]


async def test_trace_id_is_none_outside_a_request(_fake_llm: None) -> None:
    """cron·스크립트 경로 — 요청이 없으면 None. 가드는 그런 행을 각각 1회로 센다."""
    assert get_trace_id() is None
    result = await aiClient.run(
        module="brief",
        schema=_Schema,
        prompt_id="test/trace",
        fallback=_Schema(),
    )
    assert result.fell_back is True


async def test_no_turn_costs_three_llm_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ **한 턴이 LLM 3콜을 쓰면 안 된다** — 채점과 수확이 합쳐졌기 때문이다 (#431).

    예전엔 자유서술 턴이 `ask_question` + `ambiguity_score` + `slot_extraction` 3콜이었다.
    수확 여부는 LLM 을 부르기 전에 정해지므로(자유서술인가 · 20자 이상인가 · 열린 슬롯이
    있는가), 수확할 때만 합친 프롬프트를 쓰면 **한 호출로** 끝난다.

    ⚠️ **무조건 합치면 손해다.** 수확하지 않는 turn 까지 수확 규칙(약 856토큰)을 프롬프트에
    짊어져 실측 토큰이 +35% 가 된다. 조건부라야 −5% 다. 이 테스트는 그 조건부 배선이
    "합쳤는데 무조건이 됐다" 로 퇴화하지 않는지까지는 못 본다 — 그건 토큰 문제라
    `test_merged_prompt_is_used_only_when_harvesting` 이 지킨다.
    """
    requests, llm_calls = await _drive_full_plan_interview(monkeypatch)

    # 턴당 최대 2콜(질문 생성 + 답 처리). 세션 시작은 질문 1콜뿐이라 상한이 더 낮다.
    assert llm_calls <= requests * 2, (
        f"요청 {requests}건에 LLM {llm_calls}콜 — 턴당 2콜을 넘는다. "
        "채점과 수확이 다시 갈라졌는지 확인해라."
    )


async def test_merged_prompt_is_used_only_when_harvesting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """수확하지 않는 턴은 **짧은 채점 프롬프트**를 써야 한다.

    합친 프롬프트를 무조건 쓰면 요청 수는 같은데 토큰이 늘어난다(실측 +35%).
    이 테스트가 그 퇴화를 잡는다 — 완주 중 채점 호출의 **일부만** 합친 프롬프트여야 한다.
    """
    calls: list[str] = []
    monkeypatch.setattr(aiClient, "run", _counting_stub(calls))

    result = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    guard = 0
    while not result.done and guard < 40:
        slot = result.state["next_slot_key"]
        assert slot is not None
        result = await interview_runner.submit_and_advance(
            state=result.state, slot_key=slot, answer_value=_answer_for(slot)
        )
        guard += 1

    scoring = [c for c in calls if "ambiguity_score" in c or "answer_intake" in c]
    merged = [c for c in scoring if "answer_intake" in c]
    assert scoring, "채점 호출이 하나도 없다 — 드라이버가 깨졌다"
    # ⚠️ **하한이 있어야 한다.** 상한만 두면 "수확을 아예 안 한다" 는 변이가 통과한다
    # (merged=0 도 `< len(scoring)` 을 만족한다) — 실제로 그 변이가 안 잡혔다.
    assert merged, (
        "합친 프롬프트가 한 번도 안 쓰였다 — 수확이 통째로 꺼졌는지 확인해라. "
        "완주 드라이버에는 20자를 넘는 자유서술 답이 들어 있다."
    )
    assert len(merged) < len(scoring), (
        f"채점 호출 {len(scoring)}건이 **전부** 합친 프롬프트({len(merged)}건)다. "
        "무조건 합치면 수확하지 않는 턴까지 수확 규칙을 짊어져 토큰이 늘어난다."
    )
