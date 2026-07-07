"""Deep Interview Orchestrator (#6) — Rule-based Slot FSM + LLM Nodes.

흐름 (베이스라인 §6 "필수 슬롯 채우기 → 모호함 0 까지 cycle"):

    ask_question → receive_answer → validate_answer ─┐
         ▲                                           │ should_continue
         └──────────────── continue ─────────────────┤
                            finish → summarize_interview → finalize_outcome → END

설계 원칙 (요청 규칙 엄수):
- **Rule-based Slot FSM**: 다음에 물을 슬롯 선택·종료 판단은 LLM 0회의 순수 규칙
  (`_next_required_slot` / `_terminal_reason`). 룰이 흐름을 운전하고 LLM 은 문장 생성·
  채점에만 쓴다 — 8s timeout/rate limit 이 와도 인터뷰가 끊기지 않는다.
- **모든 LLM 호출은 `aiClient.run(...)` 단일 게이트만** (AGENTS.md §2). Gemini SDK 직접
  import 금지. 각 노드는 timeout=8.0 + 같은 schema 로 환원하는 룰 `fallback=` 을 넘긴다.
- **Envelope-less**: 터미널은 껍데기 없이 도메인 객체 `InterviewOutcome` 를 빌드(LLM 0회).
  요약 확인 카드(`InterviewSummary`)는 표현 계층으로 state 에만 싣는다.
- State 는 직렬화 가능해야 한다 → `AsyncSession` 은 넣지 않고
  `config["configurable"]["session"]` 채널로 전달 (ADR-0005 §7.1).

종료 조건 (FSM 완료):
  필수 슬롯 전부 충족(= 명료성 100%) / early_finish.

  ⚠️ float `ambiguity_score` 는 **종료를 운전하지 않는다**. FE 명료성 지표(= 남은 필수
  슬롯 수, API 의 `ambiguityScore`(int))와 진실 소스가 달라, float 임계로 조기 종료하면
  필수 슬롯이 다 차기 전에 끝나 명료성이 100%에 못 닿는다. 완료는 슬롯 충족(FSM)이 단독으로
  운전하고, float 값은 telemetry(`ambiguity_final`)로만 남긴다.

  루프 방지는 **슬롯별 시도 상한**(`_decide_storage`/`MAX_SLOT_ATTEMPTS`, pending 마커로
  영속)이 담당한다 — 상한에 닿으면 그 슬롯을 스킵/best-effort 로 채워 진행시켜, 같은 질문이
  무한 반복되지 않고 모든 슬롯이 결국 채워져 완료로 수렴한다(별도 turn_limit 불필요).

라우터는 보통 그래프를 한 번에 `ainvoke` 하지 않고 `interview_runner` 로 턴 단위 구동한다
(사용자 답이 HTTP 요청으로 외부에서 들어오기 때문 — `receive_answer` 가 no-op 인 이유).
"""

from __future__ import annotations

import re
from typing import Any, Literal, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from reaction_backend.llm import aiClient
from reaction_backend.orchestrator import interview_adapter
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.interview import (
    AmbiguityUpdate,
    InterviewEndReason,
    InterviewOutcome,
    InterviewSummary,
    NextQuestionSchema,
    SlotHarvest,
)

__all__ = [
    "InterviewState",
    "ask_question",
    "build_interview_graph",
    "finalize_outcome",
    "harvest_slots",
    "initial_state",
    "receive_answer",
    "should_continue",
    "summarize_interview",
    "validate_answer",
]

STORE_CLARITY_MIN = 0.4  # clarity 가 이 미만이면 답을 채우지 않고 같은 슬롯 재질문

# 사용자가 '없음/모름/건너뛰기'를 밝힌 슬롯에 저장하는 스킵 마커(빈 text). build_outcome 은
# 이를 값 없음(default)으로 읽고, FSM 은 '채워짐'으로 보아 다음 슬롯으로 진행 → 무한 재질문 방지.
_SKIP_MARKER: dict[str, Any] = {"type": "text", "raw": ""}

# 핵심 목표 슬롯 — 계획의 근간이라 '없어/모름' 스킵을 받지 않고, 유효한 답이 나올 때까지
# (상한 내에서) 재질문한다. 비핵심 슬롯은 스킵/제약-무루프로 곧장 진행.
CRITICAL_SLOTS: frozenset[str] = frozenset({"goals.list", "goals.heaviest"})

# 한 슬롯에 허용하는 최대 시도 횟수(최초 1 + 재질문 2). 이후엔 어쩔 수 없이 진행:
# 핵심 슬롯은 마지막 비지 않은 답을 best-effort 로 채택, 비핵심은 스킵(default).
MAX_SLOT_ATTEMPTS = 3

# 하베스팅(slot_extraction) — 이 신뢰도 미만은 미리 채우지 않고 정식 질문으로 넘긴다.
# 잘못 채우면 사용자가 정정 기회를 잃어 재질문보다 나쁘므로 보수적으로.
HARVEST_MIN_CONFIDENCE = 0.7
# 하베스팅 대상에서 제외 — goals.heaviest 는 goals.list 응답에서 파생(동적 보기)이라 별도.
_HARVEST_EXCLUDE: frozenset[str] = frozenset({"goals.heaviest"})


def _pending(attempts: int) -> dict[str, Any]:
    """재질문 대기 마커 — 시도 횟수를 slot_answers 에 실어 턴 사이에 영속(스키마 변경 없이)."""
    return {"type": "pending", "attempts": attempts}


def _pending_attempts(value: dict[str, Any] | None) -> int:
    """슬롯에 누적된 시도 횟수 (pending 마커면 그 값, 아니면 0)."""
    if value and value.get("type") == "pending":
        raw = value.get("attempts", 0)
        return int(raw) if isinstance(raw, int) else 0
    return 0


def _retry_hint(slot_key: str, attempts: int) -> str:
    """재질문 힌트 — 같은 질문 반복이 아니라 직전 답이 왜 부족했는지 짚고 더 구체적으로 묻게 한다."""
    if attempts <= 0:
        return ""
    if slot_key in CRITICAL_SLOTS:
        return (
            "재질문: 직전 답으로는 이 항목을 정하기 어려웠다. 이건 계획의 핵심이라 건너뛸 수 없으니, "
            "직전 답을 짧게 되짚고 보기·예시를 들어 고르기 쉽게 다시 물어라."
        )
    return "재질문: 직전 답이 조금 모호했다. 같은 말 반복 말고 예시·보기를 들어 답하기 쉽게 물어라."


# Rule-based FSM 이 순서대로 채워가는 필수 슬롯 (interview_adapter 와 동일 진실 소스).
# 핵심 목표(goals.*) / 가용 시간(time.*) / 선호 방식(recovery.*) 그룹을 모두 포함.
REQUIRED_SLOT_SEQUENCE: tuple[str, ...] = interview_adapter.REQUIRED_SLOT_KEYS

# 러닝 컨텍스트용 짧은 태그 — 앞서 답한 슬롯을 다음 질문 프롬프트에 실어(ask_question) LLM 이
# 이전 답을 이어받아 자연스럽게 묻게 한다(맥락 없이 슬롯키만 보고 추측하던 문제 보완).
_CONTEXT_LABELS: dict[str, str] = {
    "identity.role": "학년/시기",
    "identity.season": "학기",
    "goals.list": "목표",
    "goals.heaviest": "가장 무거운 목표",
    "goals.deadlines": "마감",
    "goals.success_image": "이번 주 목표 모습",
    "time.activity_window": "활동 시간대",
    "time.peak_window": "집중 시간대",
    "time.no_touch": "노터치",
    "recovery.tone": "회복 톤",
    "recovery.rest_ok": "휴식 수용",
    "recovery.downscope_unit": "최소 실행 단위",
}


class InterviewState(TypedDict):
    """LangGraph 가 Node 간 전달하는 상태. DB(`interview_sessions`)와 별도 short-lived.

    직렬화 가능해야 하므로 비직렬화 객체(AsyncSession 등)는 넣지 않는다(ADR-0005 §7.1).
    """

    # 식별/진행
    session_id: UUID
    user_id: UUID
    ambiguity_score: float  # 0..1, 낮을수록 명확 (DB ambiguity_final 과 동일 척도)
    total_turns: int
    early_finish: bool  # [충분해요] 탭
    end_reason: InterviewEndReason | None

    # 턴 단위
    next_slot_key: str | None  # FSM 이 이번 턴에 물은 필수 슬롯
    last_slot_key: str | None  # 직전 답이 속한 슬롯 (라우터가 주입)
    last_answer: dict[str, Any] | None  # interview_slot_answers.value 형태
    next_question: NextQuestionSchema | None
    used_fallback: bool  # 어느 턴이든 룰 정규화면 True → outcome.analysis_source

    # 누적 슬롯 (DB slot_answers 의 in-memory 미러) {slot_key: value}
    slot_answers: dict[str, dict[str, Any] | None]

    # 이번 턴에 하베스팅으로 미리 채운 슬롯키들 (transient — 응답 표시용, 영속 대상 아님)
    harvested: list[str]

    # 터미널 산출물
    summary: InterviewSummary | None  # 요약 확인 카드 (표현 계층)
    outcome: InterviewOutcome | None  # 경계 계약 (First Plan 시드)


def initial_state(*, session_id: UUID, user_id: UUID) -> InterviewState:
    """라우터/테스트에서 그래프 진입 시 쓰는 초기 상태."""
    return InterviewState(
        session_id=session_id,
        user_id=user_id,
        ambiguity_score=1.0,
        total_turns=0,
        early_finish=False,
        end_reason=None,
        next_slot_key=None,
        last_slot_key=None,
        last_answer=None,
        next_question=None,
        used_fallback=False,
        slot_answers={},
        harvested=[],
        summary=None,
        outcome=None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rule-based FSM helpers — 순수 함수 (LLM 호출 X). 흐름은 룰이 운전한다.
# ─────────────────────────────────────────────────────────────────────────────


def _is_filled(value: dict[str, Any] | None) -> bool:
    """슬롯 값이 실질적으로 채워졌는지 (빈 dict/None/pending 마커 제외)."""
    return interview_adapter.is_filled_answer(value)


def _next_required_slot(state: InterviewState) -> str | None:
    """아직 안 채운 첫 필수 슬롯 키. 모두 채웠으면 None (FSM 완료 신호)."""
    answers = state["slot_answers"]
    return next((k for k in REQUIRED_SLOT_SEQUENCE if not _is_filled(answers.get(k))), None)


def _all_required_filled(state: InterviewState) -> bool:
    return _next_required_slot(state) is None


# ─────────────────────────────────────────────────────────────────────────────
# 룰 fallback (8s timeout / rate limit / schema 실패 시) — 같은 schema 로 환원.
# ─────────────────────────────────────────────────────────────────────────────


def _rule_next_question(state: InterviewState, slot_key: str) -> NextQuestionSchema:
    """카탈로그 기본 질문으로 회귀 — LLM 죽어도 인터뷰가 끊기지 않는다."""
    return NextQuestionSchema(
        question=_DEFAULT_SLOT_QUESTIONS.get(
            slot_key, "조금만 더 구체적으로 알려주실 수 있을까요?"
        ),
        empathy_one_liner="천천히 알려주셔도 괜찮아요.",
    )


def _rule_ambiguity_update(state: InterviewState, slot_key: str) -> AmbiguityUpdate:
    """답이 있으면 모호함을 소폭 감소시키는 단순 휴리스틱."""
    answered = _has_answer_text(state)
    new_score = max(0.0, state["ambiguity_score"] - (0.15 if answered else 0.0))
    return AmbiguityUpdate(
        slot_key=slot_key,
        clarity_score=0.5 if answered else 0.0,
        new_ambiguity=new_score,
    )


def _rule_summary(state: InterviewState) -> InterviewSummary:
    """슬롯에서 결정적으로 빌드한 룰 요약 — LLM 실패 시 그대로 노출.

    LLM 요약과 같은 슬롯 소스를 쓰되, 값이 있는 항목(마감·성공 이미지·노터치·휴식·다운스코프)만
    골라 문장에 덧붙인다 — "아직 정하지 않음" 은 지어내지 않으려 생략.
    """
    v = _summary_variables(state)
    goals = v["goals"]

    goal_summary = f"가장 무겁게 느끼는 일은 '{v['heaviest']}' 이고, 정리한 목표는 {goals} 예요."
    if v["deadlines"] != _NOT_SET:
        goal_summary += f" 마감은 {v['deadlines']} 예요."
    if v["success_image"] != _NOT_SET:
        goal_summary += f" 이번 주엔 '{v['success_image']}' 모습을 그리셨어요."

    time_summary = (
        f"활동 시간대는 {v['time_window']}, 집중은 {v['peak_window']} 가 좋다고 하셨어요."
    )
    if v["no_touch"] != _NOT_SET:
        time_summary += f" '{v['no_touch']}' 시간은 비워둘게요."

    preference_summary = f"못 한 날엔 '{v['tone']}' 톤을 선호하세요."
    if v["rest_ok"] != _NOT_SET:
        preference_summary += f" 휴식 제안은 '{v['rest_ok']}'."
    if v["downscope_unit"] != _NOT_SET:
        preference_summary += f" 밀리면 {v['downscope_unit']} 단위로 줄여볼게요."

    return InterviewSummary(
        headline=f"{v['identity']} · 핵심 목표 {goals}",
        goal_summary=goal_summary,
        time_summary=time_summary,
        preference_summary=preference_summary,
        confirm_question="이대로 계획을 세워볼까요?",
    )


def _session(config: RunnableConfig) -> Any:
    """config["configurable"]["session"] 안전 추출 (없으면 None → 예산/로깅 skip)."""
    return config.get("configurable", {}).get("session")


def _tone_mode(config: RunnableConfig) -> str | None:
    """config["configurable"]["tone_mode"] 안전 추출 (#23-D). 없으면 None = 톤 prefix 없음."""
    raw = config.get("configurable", {}).get("tone_mode")
    return raw if isinstance(raw, str) else None


def _answer_type(config: RunnableConfig) -> str | None:
    """직전 답 슬롯의 answer_type (라우터가 카탈로그에서 주입). 정규화 추출 지시에 사용."""
    raw = config.get("configurable", {}).get("answer_type")
    return raw if isinstance(raw, str) else None


def _answer_options(config: RunnableConfig) -> list[str]:
    """직전 답 슬롯의 chip/select 보기 (라우터 주입). LLM 이 자유서술을 보기로 매핑하게 한다."""
    raw = config.get("configurable", {}).get("options")
    return [str(x) for x in raw] if isinstance(raw, list) else []


def _slot_meta(config: RunnableConfig) -> dict[str, dict[str, Any]]:
    """슬롯키→{label, answer_type, options} 맵 (라우터가 카탈로그에서 주입).

    ask_question 이 이번에 물을 슬롯의 사람용 라벨·형식·보기를 프롬프트에 실어, LLM 이
    슬롯 의도에 정확히 맞는 질문을 만들게 한다(없으면 키 문자열만 보고 추측하던 문제).
    """
    raw = config.get("configurable", {}).get("slot_meta")
    return raw if isinstance(raw, dict) else {}


# ─────────────────────────────────────────────────────────────────────────────
# Nodes — async def node(state, config). config 두 번째 인자 (ADR-0005 §7.1).
# ─────────────────────────────────────────────────────────────────────────────


async def ask_question(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """LLM ① — FSM 이 고른 다음 필수 슬롯에 대한 질문 1개 생성.

    슬롯 선택은 룰(`_next_required_slot`), 문장만 LLM. timeout 시 카탈로그 기본 질문.
    """
    slot_key = _next_required_slot(state) or ""
    meta = _slot_meta(config).get(slot_key) or {}
    meta_options = meta.get("options") or []
    attempts = _pending_attempts(state["slot_answers"].get(slot_key))  # 이 슬롯 재질문 횟수
    result = await aiClient.run(
        module="interview",
        schema=NextQuestionSchema,
        prompt_id="interview/next_question",
        fallback=lambda: _rule_next_question(state, slot_key),
        timeout=8.0,
        variables={
            "goal_title": _heaviest_goal_hint(state),
            "answered_context": _answered_context(state),
            "ambiguous_slot": slot_key,
            # 슬롯 의도(라벨)·형식·보기를 실어 LLM 이 정확한 질문을 만들게 한다.
            "slot_label": str(meta.get("label") or _DEFAULT_SLOT_QUESTIONS.get(slot_key, slot_key)),
            "answer_type": str(meta.get("answer_type") or "text"),
            "options": ", ".join(str(o) for o in meta_options) or "(자유 입력)",
            "last_answer": _last_answer_text(state),
            "retry": _retry_hint(slot_key, attempts),
        },
        user_id=state["user_id"],
        session=_session(config),
        tone_mode=_tone_mode(config),
    )
    return {
        **state,
        "next_question": result.value,
        "next_slot_key": slot_key,
        "total_turns": state["total_turns"] + 1,
        "used_fallback": state["used_fallback"] or result.fell_back,
    }


async def receive_answer(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """사용자 답 수신 노드 — 외부 트리거(POST .../answers)로 진입.

    실제 답 주입·DB UPSERT 는 라우터(`interview_runner.submit_and_advance`)가 한다.
    그래프 자체를 batch `ainvoke` 할 때는 답이 없으므로 no-op (state passthrough).
    """
    return state


async def validate_answer(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """LLM ② — 직전 답을 채점·정규화(`interview/ambiguity_score`)하고 슬롯에 저장한다.

    실제 저장 결정(무엇을 저장하고 채워졌다고 볼지)은 순수 함수 `_decide_storage` 가 맡는다
    (표로 단위 테스트 가능). 이 노드는 LLM 호출·상태 조립만 한다.

    ⚠️ chip 을 clarity 게이트에 태우면 안 되는 이유: 실 LLM 이 "1학년"·"담백" 같은 유효한
    단일 chip 선택을 0.3 정도로 낮게 채점해, 필수 chip 슬롯(13개 중 7개)이 영구 재질문에
    빠져 turn_limit 로 끝나고 명료성이 0% 에 갇힌다. 명확성 판단이 필요한 건 자유 서술뿐이다.
    """
    slot_key = state.get("last_slot_key") or state.get("next_slot_key") or ""
    answer_type = _answer_type(config)

    result = await aiClient.run(
        module="interview",
        schema=AmbiguityUpdate,
        prompt_id="interview/ambiguity_score",
        fallback=lambda: _rule_ambiguity_update(state, slot_key),
        timeout=8.0,
        variables={
            "slot_key": slot_key,
            "answer": _last_answer_text(state),
            "answer_type": answer_type or "text",
            "options": ", ".join(_answer_options(config)) or "(자유 입력)",
            "today": now_kst().date().isoformat(),
        },
        user_id=state["user_id"],
        session=_session(config),
        tone_mode=_tone_mode(config),
    )
    update = result.value

    slot_answers = dict(state["slot_answers"])
    attempts = _pending_attempts(slot_answers.get(slot_key)) + 1  # 이번 시도 포함
    stored, filled_now = _decide_storage(
        slot_key,
        answer_type,
        state["last_answer"],
        update.normalized_value,
        update.clarity_score,
        attempts,
    )
    if stored is not None:  # 실제 값·스킵·pending 모두 저장(영속) — pending 은 '미충족'으로 읽힘
        slot_answers[slot_key] = stored
    # 목표가 1개뿐이면 goals.heaviest 자동 채움 → 자명한 select 질문(직전 답 echo)을 건너뛴다.
    if filled_now and slot_key == "goals.list":
        _autofill_single_goal_heaviest(slot_answers)

    return {
        **state,
        "slot_answers": slot_answers,
        "ambiguity_score": update.new_ambiguity,  # telemetry(ambiguity_final) — 종료는 FSM 이 운전
        "last_answer": None,  # 소비 완료 — 다음 턴 답과 섞이지 않게
        "last_slot_key": None,
        "used_fallback": state["used_fallback"] or result.fell_back,
    }


def _harvest_slot_line(slot_key: str, meta: dict[str, Any]) -> str:
    """하베스팅 프롬프트에 실을 '미충족 슬롯' 한 줄 (key | 라벨 | 형식 | 보기)."""
    label = str(meta.get("label") or _DEFAULT_SLOT_QUESTIONS.get(slot_key, slot_key))
    answer_type = str(meta.get("answer_type") or "text")
    opts = meta.get("options") or []
    opts_str = ", ".join(str(o) for o in opts) or "(자유 입력)"
    return f"- {slot_key} | {label} | {answer_type} | {opts_str}"


async def harvest_slots(
    state: InterviewState,
    config: RunnableConfig,
    *,
    answer_text: str,
    answered_slot: str,
) -> InterviewState:
    """LLM(선택) — 직전 자유서술 답에서 **다른 미충족 슬롯**을 함께 추출해 미리 채운다.

    사용자가 한 답에 여러 항목을 흘렸을 때(예: "3학년 방학이고 캡스톤 8월 마감") 같은 걸 다시
    묻지 않도록, 아직 비어 있는 슬롯을 confidence 게이트(`HARVEST_MIN_CONFIDENCE`)로만 미리
    채운다. runner 가 자유서술 답일 때만 호출한다(chip/range 는 단일 구조화 값이라 무의미).

    실패/빈 추출이면 아무것도 안 채운다(빈 배열 fallback). 이미 채워진 슬롯은 덮지 않는다.
    """
    open_slots = [
        k
        for k in REQUIRED_SLOT_SEQUENCE
        if k != answered_slot
        and k not in _HARVEST_EXCLUDE
        and not _is_filled(state["slot_answers"].get(k))
    ]
    if not open_slots or not answer_text.strip():
        return {**state, "harvested": []}

    meta = _slot_meta(config)
    listing = "\n".join(_harvest_slot_line(k, meta.get(k) or {}) for k in open_slots)
    result = await aiClient.run(
        module="interview",
        schema=SlotHarvest,
        prompt_id="interview/slot_extraction",
        fallback=lambda: SlotHarvest(slots=[]),
        timeout=8.0,
        variables={
            "answer": answer_text,
            "answered_slot": answered_slot,
            "today": now_kst().date().isoformat(),
            "open_slots": listing,
        },
        user_id=state["user_id"],
        session=_session(config),
        tone_mode=_tone_mode(config),
    )

    slot_answers = dict(state["slot_answers"])
    open_set = set(open_slots)
    prefilled: list[str] = []
    for h in result.value.slots:
        if h.slot_key not in open_set or _is_filled(slot_answers.get(h.slot_key)):
            continue
        if h.confidence < HARVEST_MIN_CONFIDENCE:
            continue
        answer_type = (meta.get(h.slot_key) or {}).get("answer_type")
        stored = _coerce_normalized(
            answer_type if isinstance(answer_type, str) else None, h.normalized_value
        )
        if stored is not None:
            slot_answers[h.slot_key] = stored
            prefilled.append(h.slot_key)

    return {
        **state,
        "slot_answers": slot_answers,
        "used_fallback": state["used_fallback"] or result.fell_back,
        "harvested": prefilled,
    }


async def summarize_interview(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """LLM ③ — 모은 슬롯을 요약 확인 카드로. timeout 시 슬롯에서 룰 요약."""
    v = _summary_variables(state)
    result = await aiClient.run(
        module="interview",
        schema=InterviewSummary,
        prompt_id="interview/summary",
        fallback=lambda: _rule_summary(state),
        timeout=8.0,
        variables=v,
        user_id=state["user_id"],
        session=_session(config),
        tone_mode=_tone_mode(config),
    )
    return {
        **state,
        "summary": result.value,
        "used_fallback": state["used_fallback"] or result.fell_back,
    }


async def finalize_outcome(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """터미널 — LLM 0회로 경계 계약(InterviewOutcome) 빌드. First Plan 시드."""
    reason: InterviewEndReason = _terminal_reason(state) or "completed"
    outcome = interview_adapter.build_outcome(
        session_id=str(state["session_id"]),
        slot_answers=state["slot_answers"],
        ambiguity_final=state["ambiguity_score"],
        end_reason=reason,
        analysis_source="rule" if state["used_fallback"] else "llm",
    )
    return {**state, "outcome": outcome, "end_reason": reason}


# ─────────────────────────────────────────────────────────────────────────────
# Conditional edge — 순수 함수 (LLM 호출 X, ADR-0005 §2.4 패턴).
# ─────────────────────────────────────────────────────────────────────────────


def _terminal_reason(state: InterviewState) -> InterviewEndReason | None:
    """종료 조건 평가. 종료면 DB enum 사유, 아니면 None.

    완료는 **필수 슬롯 완료(FSM)가 단독으로 운전**한다 — 이때만 남은 필수 슬롯 0(= FE
    명료성 100%)이 보장된다. float `ambiguity_score` 가 낮아도 미해결 필수 슬롯이 남으면
    계속 묻는다. 재질문 폭주는 `_decide_storage` 의 슬롯별 시도 상한이 막아 모든 슬롯이 결국
    채워지므로, 별도 turn_limit 없이도 완료로 수렴한다.
    """
    if state["early_finish"]:
        return "early_user"
    if _all_required_filled(state):
        return "completed"
    return None


def should_continue(state: InterviewState) -> Literal["continue", "finish"]:
    """Cycle 종료 조건. 종료면 summarize_interview, 아니면 ask_question 재진입."""
    return "finish" if _terminal_reason(state) is not None else "continue"


# ─────────────────────────────────────────────────────────────────────────────
# 답 정규화 / 프롬프트 변수 보조
# ─────────────────────────────────────────────────────────────────────────────

_TEXT_SPLIT_RE = re.compile(r"[,、，\n]")

# '없음/모름/건너뛰기' 의사 표현 — LLM 이 normalized_value="" 신호를 놓쳐도(짧은 "없어" 등)
# 룰로 스킵 처리해 무한 재질문을 막는 백스톱.
_SKIP_RE = re.compile(
    r"없어|없음|없다|없습니다|모르|몰라|상관\s*없|딱히|건너|넘어갈|해당\s*없|스킵|skip",
    re.IGNORECASE,
)


def _looks_like_skip(text: str) -> bool:
    """항목 없음·건너뛰기 의사가 답의 거의 전부인지 (룰 백스톱).

    긴 답에 우연히 '없어'가 섞인 경우(예: "고정 시간 없어서 자유로워")는 제외하려고 길이 상한.
    """
    t = text.strip()
    return bool(t) and len(t) <= 20 and _SKIP_RE.search(t) is not None


def _normalize_for_store(slot_key: str, answer: dict[str, Any]) -> dict[str, Any]:
    """저장 직전 룰 정규화 — text 답은 항목 리스트(`normalized`)를 채워 어댑터가 쓰기 쉽게.

    chip/range 는 그대로 둔다. 이미 normalized 가 있으면 보존.
    """
    if answer.get("type") == "text" and "normalized" not in answer:
        raw = str(answer.get("raw", ""))
        parts = [p.strip() for p in _TEXT_SPLIT_RE.split(raw) if p.strip()]
        if parts:
            return {**answer, "normalized": parts}
    return answer


# 구조화 슬롯 — 추출값만 있으면 clarity 게이트 없이 저장(선택/구간/날짜는 재질문 대상 아님).
_CONSTRAINED_TYPES = {"chip", "select", "time_range", "date_picker"}


def _coerce_normalized(answer_type: str | None, norm: Any) -> dict[str, Any] | None:
    """LLM 이 뽑은 normalized_value 를 슬롯 형식대로 저장 형태(dict)로 환원. 불가면 None.

    build_outcome 이 읽는 규약과 일치:
    - chip/select → {"type":"chip","values":[...]}
    - time_range  → {"type":"range","start":"HH:MM","end":"HH:MM"}
    - date_picker → {"type":"text","raw":"YYYY-MM-DD"}  (goals.deadlines 는 _text_raw 로 읽음)
    - text/미지정 → {"type":"text","raw":..., "normalized":[...]}
    """
    if norm is None:
        return None
    if answer_type in {"chip", "select"}:
        vals = norm if isinstance(norm, list) else [norm]
        cleaned = [str(v).strip() for v in vals if str(v).strip()]
        return {"type": "chip", "values": cleaned} if cleaned else None
    if answer_type == "time_range":
        if isinstance(norm, dict):
            start, end = norm.get("start"), norm.get("end")
            if isinstance(start, str) and start and isinstance(end, str) and end:
                return {"type": "range", "start": start, "end": end}
        return None
    if answer_type == "date_picker":
        if isinstance(norm, (dict, list)):
            return None
        s = str(norm).strip()
        return {"type": "text", "raw": s} if s else None
    # text 또는 answer_type 미지정 (graph/legacy) — 정리된 핵심값
    if isinstance(norm, list):
        items = [str(v).strip() for v in norm if str(v).strip()]
        return {"type": "text", "raw": ", ".join(items), "normalized": items} if items else None
    s = str(norm).strip()
    return {"type": "text", "raw": s} if s else None


def _resolve_stored_value(
    slot_key: str,
    answer_type: str | None,
    last_answer: dict[str, Any] | None,
    normalized: Any,
) -> tuple[dict[str, Any] | None, bool]:
    """저장할 값과 is_constrained 를 결정.

    우선순위: LLM 정규화값 → 이미 구조화된 raw(chip/range) → text raw.
    구조화 슬롯인데 어느 것도 못 얻으면 (None, True) → 저장 안 함(재질문). text 슬롯은
    원문 저장으로 폴백해 clarity 게이트가 판단하게 한다.
    """
    raw_type = last_answer.get("type") if last_answer else None
    raw_structured = raw_type in {"chip", "range"}
    is_constrained = answer_type in _CONSTRAINED_TYPES or raw_structured

    norm = _coerce_normalized(answer_type, normalized)
    if norm is not None:
        return norm, is_constrained
    if raw_structured and last_answer is not None:
        return _normalize_for_store(slot_key, last_answer), is_constrained
    if not is_constrained and last_answer is not None:
        return _normalize_for_store(slot_key, last_answer), is_constrained
    return None, is_constrained


def _decide_storage(
    slot_key: str,
    answer_type: str | None,
    last_answer: dict[str, Any] | None,
    normalized: Any,
    clarity: float,
    attempts: int,
) -> tuple[dict[str, Any] | None, bool]:
    """직전 답을 어떻게 저장할지 결정하는 **순수 함수** — `(stored, filled_now)`.

    LLM 없이 표로 단위 테스트할 수 있도록 validate_answer 의 분기를 여기로 모은다.
    - 답 미주입(배치 그래프): (None, False).
    - 유효한 구조화/자유서술 값(has_real): 곧바로 저장.
    - 핵심 목표 슬롯(CRITICAL_SLOTS): '없어/모름' 스킵 불가 → 상한까지 재질문(pending),
      상한(MAX_SLOT_ATTEMPTS) 도달 시 마지막 비지 않은 답을 best-effort 로 채택.
    - 비핵심: 스킵 의사·제약 슬롯·상한 도달이면 스킵(default)로 진행, 아니면 재질문(pending).

    `attempts` 는 이번 시도 포함 누적 횟수(pending 마커에서 복원). pending 은 '미충족'으로
    읽혀(FSM 이 같은 슬롯 재질문) 시도 횟수를 턴 사이에 나른다.
    """
    if last_answer is None:
        return None, False  # 배치 그래프 등 답 미주입 턴

    answer_text = _answer_text(last_answer)
    llm_skip = isinstance(normalized, str) and not normalized.strip()

    if llm_skip:
        real_value: dict[str, Any] | None = None
        is_constrained = answer_type in _CONSTRAINED_TYPES or last_answer.get("type") in {
            "chip",
            "range",
        }
    else:
        real_value, is_constrained = _resolve_stored_value(
            slot_key, answer_type, last_answer, normalized
        )
    has_real = real_value is not None and (is_constrained or clarity >= STORE_CLARITY_MIN)

    if has_real:
        return real_value, True
    if slot_key in CRITICAL_SLOTS:
        # 핵심 목표 — 스킵 불가. 상한 도달 & 비지 않은 답이면 best-effort, 아니면 재질문.
        if attempts >= MAX_SLOT_ATTEMPTS and answer_text.strip():
            return {"type": "text", "raw": answer_text.strip()}, True
        return _pending(attempts), False
    if llm_skip or is_constrained or _looks_like_skip(answer_text) or attempts >= MAX_SLOT_ATTEMPTS:
        return _SKIP_MARKER, True
    return _pending(attempts), False


def _has_answer_text(state: InterviewState) -> bool:
    return bool(_last_answer_text(state))


def _last_answer_text(state: InterviewState) -> str:
    return _answer_text(state["last_answer"])


def _answer_text(answer: dict[str, Any] | None) -> str:
    """답 value(dict) → 사람이 읽는 문자열 (프롬프트·스킵 감지용)."""
    if not answer:
        return ""
    if answer.get("type") == "text":
        return str(answer.get("raw", ""))
    if answer.get("type") == "chip":
        values = answer.get("values") or []
        return ", ".join(str(v) for v in values)
    if answer.get("type") == "range":
        return f"{answer.get('start', '')}~{answer.get('end', '')}"
    return ""


def _answered_context(state: InterviewState) -> str:
    """앞서 채워진 슬롯 → 다음 질문용 짧은 러닝 요약("태그=값 / …").

    아직 답이 없으면 명시 문구. LLM 이 이전 답을 이어받아(맥락 반복 없이) 자연스럽게 묻게 한다.
    """
    answers = state["slot_answers"]
    parts: list[str] = []
    for slot_key, tag in _CONTEXT_LABELS.items():
        value = answers.get(slot_key)
        if not _is_filled(value):
            continue
        text = _answer_text(value).strip()
        if text:
            parts.append(f"{tag}={text}")
    return " / ".join(parts) if parts else "(아직 답한 내용 없음)"


def _heaviest_goal_hint(state: InterviewState) -> str:
    heaviest = state["slot_answers"].get("goals.heaviest")
    text = _slot_text(heaviest) or _slot_first_chip(heaviest)
    if text:
        return text
    goals = state["slot_answers"].get("goals.list")
    items = _slot_items(goals)
    return items[0] if items else "당신의 목표"


_NOT_SET = "아직 정하지 않음"


def _summary_variables(state: InterviewState) -> dict[str, str]:
    """요약 프롬프트 변수 — 슬롯에서 사람이 읽을 문자열로 추출 (룰).

    확인 카드(Analysis Confirm)가 사용자가 실제로 답한 내용을 최대한 반영하도록, 목표·시간뿐
    아니라 마감·성공 이미지·노터치·휴식 수용·다운스코프 단위까지 함께 싣는다(빈 항목은
    "아직 정하지 않음"). 미입력 항목을 지어내지 않게 프롬프트가 이 default 를 그대로 노출.
    """
    answers = state["slot_answers"]
    role = _slot_first_chip(answers.get("identity.role")) or "미상"
    season = _slot_first_chip(answers.get("identity.season")) or ""
    goals = ", ".join(_slot_items(answers.get("goals.list"))) or _NOT_SET
    heaviest = (
        _slot_text(answers.get("goals.heaviest"))
        or _slot_first_chip(answers.get("goals.heaviest"))
        or _NOT_SET
    )
    deadlines = _slot_text(answers.get("goals.deadlines")) or _NOT_SET
    success_image = _slot_text(answers.get("goals.success_image")) or _NOT_SET
    window = answers.get("time.activity_window")
    time_window = (
        f"{window.get('start')}~{window.get('end')}"
        if window and window.get("type") == "range"
        else _NOT_SET
    )
    peak = ", ".join(_slot_chips(answers.get("time.peak_window"))) or _NOT_SET
    no_touch = ", ".join(_slot_chips(answers.get("time.no_touch"))) or _NOT_SET
    tone = _slot_first_chip(answers.get("recovery.tone")) or "담백"
    rest_ok = _slot_first_chip(answers.get("recovery.rest_ok")) or _NOT_SET
    downscope_unit = _slot_first_chip(answers.get("recovery.downscope_unit")) or _NOT_SET
    identity = f"{role} {season}".strip()
    return {
        "identity": identity,
        "goals": goals,
        "heaviest": heaviest,
        "deadlines": deadlines,
        "success_image": success_image,
        "time_window": time_window,
        "peak_window": peak,
        "no_touch": no_touch,
        "tone": tone,
        "rest_ok": rest_ok,
        "downscope_unit": downscope_unit,
    }


def _slot_chips(value: dict[str, Any] | None) -> list[str]:
    if not value or value.get("type") != "chip":
        return []
    raw = value.get("values") or []
    return [str(v) for v in raw] if isinstance(raw, list) else []


def _slot_first_chip(value: dict[str, Any] | None) -> str | None:
    chips = _slot_chips(value)
    return chips[0] if chips else None


def _slot_text(value: dict[str, Any] | None) -> str | None:
    if not value or value.get("type") != "text":
        return None
    raw = value.get("raw")
    return str(raw) if isinstance(raw, str) and raw.strip() else None


def _slot_items(value: dict[str, Any] | None) -> list[str]:
    if not value or value.get("type") != "text":
        return []
    norm = value.get("normalized")
    if isinstance(norm, list):
        return [str(v) for v in norm if str(v).strip()]
    raw = value.get("raw")
    return [str(raw)] if isinstance(raw, str) and raw.strip() else []


def _autofill_single_goal_heaviest(slot_answers: dict[str, dict[str, Any] | None]) -> None:
    """목표가 1개뿐이면 goals.heaviest 를 그 목표로 자동 채워 자명한 select 질문을 건너뛴다.

    heaviest 는 '어느 목표가 가장 무거운가'를 고르는 select 인데, 목표가 하나면 선택지가 없어
    보기가 직전 답(goals.list)을 그대로 반복(echo)한다 → 그 하나를 heaviest 로 자동 확정한다.
    사용자가 이미 답한 경우(재조립 등)엔 건드리지 않는다.
    """
    if _is_filled(slot_answers.get("goals.heaviest")):
        return
    items = _slot_items(slot_answers.get("goals.list"))
    if len(items) == 1:
        slot_answers["goals.heaviest"] = {"type": "chip", "values": [items[0]]}


# 카탈로그 기본 질문 (LLM 죽었을 때 회귀) — mock.interview.SLOT_CATALOG 라벨 기반.
_DEFAULT_SLOT_QUESTIONS: dict[str, str] = {
    "identity.role": "어떤 학년/시기예요?",
    "identity.season": "지금 학기 중이에요, 방학이에요?",
    "goals.list": "지금 머릿속에 있는 일들을 편하게 알려주세요.",
    "goals.heaviest": "그중 가장 무겁게 느끼는 건 어떤 거예요?",
    "goals.deadlines": "마감일이 정해진 게 있어요?",
    "goals.success_image": "이번 주 끝에 어떤 모습이면 좋을까요?",
    "time.activity_window": "보통 몇 시부터 몇 시까지 활동해요?",
    "time.fixed_blocks": "매주 고정으로 비워야 하는 시간 있어요?",
    "time.peak_window": "가장 잘 집중되는 시간대는요?",
    "time.no_touch": "절대 일정 잡으면 안 되는 시간은요?",
    "recovery.tone": "못 한 날 어떤 톤이 좋아요?",
    "recovery.rest_ok": "쉬는 게 어때요 하는 제안을 받을 의향 있어요?",
    "recovery.downscope_unit": "밀렸을 때 할 일을 몇 분짜리까지 줄이면 해볼 만해요?",
}


def build_interview_graph() -> CompiledStateGraph[
    InterviewState, Any, InterviewState, InterviewState
]:
    """Cyclic StateGraph 컴파일. 라우터는 보통 `interview_runner` 로 턴 단위 구동하고,
    batch 시뮬레이션/테스트는 `await graph.ainvoke(initial, config=...)`."""
    graph = StateGraph(InterviewState)
    graph.add_node("ask_question", ask_question)
    graph.add_node("receive_answer", receive_answer)
    graph.add_node("validate_answer", validate_answer)
    graph.add_node("summarize_interview", summarize_interview)
    graph.add_node("finalize_outcome", finalize_outcome)

    graph.set_entry_point("ask_question")
    graph.add_edge("ask_question", "receive_answer")
    graph.add_edge("receive_answer", "validate_answer")
    graph.add_conditional_edges(
        "validate_answer",
        should_continue,
        {"continue": "ask_question", "finish": "summarize_interview"},
    )
    graph.add_edge("summarize_interview", "finalize_outcome")
    graph.add_edge("finalize_outcome", END)
    return graph.compile()
