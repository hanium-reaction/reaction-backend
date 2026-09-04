"""Interview — 딥 인터뷰 (S02, api-contract §4) — #6 실배선.

mock 스텁을 걷어내고 LangGraph 인터뷰 엔진(`orchestrator/interview*`)에 연결한다.

배선 두 축:
1. **route → 엔진** — 각 핸들러가 `interview_runner` 의 턴 함수를 호출한다
   (start / submit_and_advance / finish_early). 반환은 envelope 없이 도메인 객체.
2. **영속화(상태 재조립)** — `interview_sessions` 는 상태 통짜 저장(JSON) 칸이 없으므로
   매 요청마다 스칼라(total_turns·ambiguity_final) + `interview_slot_answers` 행을 읽어
   `InterviewState` 로 재조립(`_state_from_db`)하고, 턴 후 다시 영속(`_persist_turn`)한다.

엔진 ↔ FE 스키마 번역:
- `ambiguityScore`(int) = 남은 미해결 필수 슬롯 수 (진행될수록 감소).
- `Question` = 엔진 질문 텍스트 + 슬롯 카탈로그(answer_type·options). `goals.heaviest` 보기는
  `goals.list` 응답에서 런타임 동적 생성.
- 종료 턴에는 `summary`(S03 확인 카드) + `outcome`(First Plan 시드)을 함께 싣는다.

동시성/세션 가드:
- 단일 활성 세션 + **재시작 승리(restart-wins)** — 새 세션 시작 시 진행 중 세션이 있으면
  `abandoned` 로 닫고 새로 만든다(항상 201). FE 가 sessionId 를 잃어도 재시작만으로 복구
  가능 (이전의 409 `INTERVIEW_SESSION_EXISTS` 는 sessionId 분실 시 영구 차단이었다).
- 동시성 lock(ADR-0005 §7.6) — mutating 진입점은 `user_agent_lock` 으로 보호, 다중 디바이스
  동시 진입 시 409 `AGENT_CONCURRENT_ACCESS`.

영속 상태: 슬롯별 재질문 시도 횟수는 `interview_slot_answers.value` 의 pending 마커로,
`used_fallback`(인터뷰 중 룰 fallback 있었는지 → `outcome.analysis_source`)은
`interview_sessions.used_fallback` 컬럼으로 OR 누적 영속된다.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.config import get_settings
from reaction_backend.db.models.interview_session import InterviewSession as InterviewSessionRow
from reaction_backend.db.models.interview_slot_answer import InterviewSlotAnswer
from reaction_backend.db.models.user import User
from reaction_backend.db.session import get_db
from reaction_backend.orchestrator import (
    first_plan_adapter,
    interview,
    interview_adapter,
    interview_runner,
    mandala_adapter,
    profile_memory,
    ultimate_adapter,
)
from reaction_backend.orchestrator._common import user_agent_lock
from reaction_backend.orchestrator.interview import InterviewState
from reaction_backend.orchestrator.interview_catalog import (
    CATALOGS,
)
from reaction_backend.repositories.interview_repo import InterviewRepo, get_interview_repo
from reaction_backend.repositories.profile_repo import ProfileRepo, get_profile_repo
from reaction_backend.safety import endpoint_rate_limit
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.interview import (
    InterviewEndReason,
    InterviewSession,
    Question,
    SlotAnswerRequest,
    SlotCatalogEntry,
    StartSessionRequest,
)
from reaction_backend.schemas.ultimate_goal import UltimateEndReason, UltimateGoalOutcome

router = APIRouter(prefix="/interview", tags=["interview"])

logger = logging.getLogger(__name__)


# ADR-0005 §7.6 — Interview 동시성 lock 의 agent 식별자. kind 로 스코프해 서로 다른
# 인터뷰(계획/궁극목표)가 서로를 409 로 막지 않게 한다 — 마이그레이션 불필요(advisory
# lock 은 트랜잭션 범위 해시일 뿐 영속되지 않는다).
def _lock_agent(kind: str) -> str:
    return f"interview:{kind}"


async def _persist_profile_best_effort(session: AsyncSession, *, user: User, outcome: Any) -> None:
    """지속형 프로필 메모리 영속을 **best-effort** 로 수행 (#130 리뷰).

    프로필 영속은 부가 기능이라 실패해도 인터뷰 완료(finalize)를 막으면 안 된다. savepoint
    (`begin_nested`)로 감싸 실패 시 프로필 변경만 롤백하고, 같은 트랜잭션의 목표/세션 종결은
    보존한다(부분 flush 로 세션이 깨진 채 commit 되는 것 방지). 실패는 로깅만 하고 삼킨다.
    """
    try:
        async with session.begin_nested():
            await profile_memory.persist_profile_from_outcome(session, user=user, outcome=outcome)
    except Exception:  # noqa: BLE001 — 프로필 영속 실패가 인터뷰 완료를 깨지 않게
        logger.warning("profile memory persist failed; interview finalize continues", exc_info=True)


RepoDep = Annotated[InterviewRepo, Depends(get_interview_repo)]
ProfileRepoDep = Annotated[ProfileRepo, Depends(get_profile_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]


# ─────────────────────────────────────────────────────────────────────────────
# helpers — 에러 / config / 재조립 / 매핑 / 영속화
# ─────────────────────────────────────────────────────────────────────────────


def _not_found() -> ApiError:
    return ApiError(
        ErrorCode.INTERVIEW_SESSION_NOT_FOUND,
        "해당 인터뷰 세션을 찾을 수 없어요.",
        http_status=status.HTTP_404_NOT_FOUND,
    )


def _parse_session_id(session_id: str) -> UUID:
    try:
        return UUID(session_id)
    except ValueError as e:
        raise _not_found() from e


def _config(
    session: AsyncSession, slot_meta: dict[str, dict[str, Any]] | None = None
) -> RunnableConfig:
    """노드가 예산 가드·llm_runs 기록에 쓰는 세션 채널 (ADR-0005 §7.1) + 슬롯 메타."""
    return {"configurable": {"session": session, "slot_meta": slot_meta or {}}}


def _slot_meta(
    slot_answers: Mapping[str, dict[str, Any] | None], *, kind: str = "plan"
) -> dict[str, dict[str, Any]]:
    """슬롯키→{label, answer_type, options} 맵 — ask_question 이 질문 프롬프트에 실어

    슬롯 의도(라벨)·형식·보기까지 보고 정확한 질문을 만들게 한다. goals.heaviest 보기는
    현재 slot_answers(goals.list)에서 동적 생성(plan 카탈로그 한정).
    """
    return {
        s.slot_key: {
            "label": s.label,
            "answer_type": s.answer_type,
            "options": _question_options(s.slot_key, slot_answers, kind=kind),
        }
        for s in CATALOGS[kind].slots
    }


async def _load(repo: InterviewRepo, user_id: UUID, session_id: str) -> InterviewSessionRow:
    row = await repo.get_active(user_id, _parse_session_id(session_id))
    if row is None:
        raise _not_found()
    return row


def _state_from_db(
    row: InterviewSessionRow, slot_rows: list[InterviewSlotAnswer]
) -> InterviewState:
    """interview_sessions 스칼라 + slot_answers 행 → InterviewState 재조립.

    영속 대상: slot_answers(pending 시도 마커 포함)·ambiguity·total_turns·used_fallback·kind.
    (`next_*` 만 turn-local transient 라 default 로 시작.)

    ⚠️ `kind=row.kind` 를 빠뜨리면 재조립된 state 가 항상 "plan" 으로 기본값 처리돼, 궁극목표
    세션이 두 번째 턴부터 plan 카탈로그로 질문·채점된다(카탈로그 완전 불일치).
    """
    state = interview.initial_state(session_id=row.id, user_id=row.user_id, kind=row.kind)
    state["slot_answers"] = {r.slot_key: r.value for r in slot_rows if r.value is not None}
    if row.ambiguity_final is not None:
        state["ambiguity_score"] = float(row.ambiguity_final)
    state["used_fallback"] = bool(row.used_fallback)
    state["total_turns"] = row.total_turns
    return state


def _remaining_required(
    slot_answers: Mapping[str, dict[str, Any] | None], *, kind: str = "plan"
) -> int:
    """남은 미해결 필수 슬롯 수 → FE ambiguityScore(int). pending(재질문 대기)은 미충족으로 센다.

    `kind` 별 필수 슬롯 집합이 다르므로(궁극목표 인터뷰는 계획 인터뷰와 다른 슬롯을 묻는다)
    분모가 kind 를 따라간다 — 안 그러면 진행바가 0%에 고정된 채 인터뷰만 정상 종료된다.

    ⚠️ 세는 규칙은 `open_required_keys` 단 하나다. 예전엔 여기서만 `is_filled_answer` 로
    직접 셌는데, 그러면 **다른 답에서 유도돼 묻지 않은** 슬롯(`goals.weekly_time` = 세션
    길이 × 빈도)이 영영 미충족으로 잡힌다: FSM 은 `completed`, `unresolved_slots` 는 빈
    배열인데 이 값만 1로 남아 진행바가 17/18 에 멈췄다. FSM 이 안 묻는 슬롯은 사용자가
    채울 방법이 없으므로 지표에도 세면 안 된다.
    """
    required = CATALOGS[kind].required_keys
    return len(interview_adapter.open_required_keys(required, slot_answers))


def _question_options(
    slot_key: str,
    slot_answers: Mapping[str, dict[str, Any] | None],
    *,
    kind: str = "plan",
    mandala_goal_titles: Sequence[str] = (),
) -> list[str]:
    """chip/select 보기. `goals.heaviest`(plan 전용)는 두 출처를 합쳐 동적 생성한다:
    ① 사용자가 방금 나열한 `goals.list` 응답 ② 만다라 축에서 승격해 둔 목표
    (`mandala_goal_titles`, ADR-0008 §8 "B") — 승격만 하고 `goals.list` 에 다시 타이핑
    안 해도 이번 학기 목표로 바로 고를 수 있게 한다("접합점",
    `docs/ultimate-goal-mandalart-strategy.md:71`). 승격 목표를 먼저 두고 겹치는 제목은
    한 번만 남긴다.
    """
    if slot_key == "goals.heaviest":
        seen: set[str] = set()
        options: list[str] = []
        for title in mandala_goal_titles:
            t = title.strip()
            if t and t not in seen:
                seen.add(t)
                options.append(t)
        goals = slot_answers.get("goals.list")
        typed: list[str] = []
        if isinstance(goals, dict) and goals.get("type") == "text":
            norm = goals.get("normalized")
            if isinstance(norm, list):
                typed = [str(x) for x in norm if str(x).strip()]
            else:
                raw = goals.get("raw")
                if isinstance(raw, str) and raw.strip():
                    typed = [raw.strip()]
        for t in typed:
            if t not in seen:
                seen.add(t)
                options.append(t)
        return options
    slot = CATALOGS[kind].by_key.get(slot_key)
    return list(slot.options) if slot else []


def _to_question(
    state: InterviewState, *, mandala_goal_titles: Sequence[str] = ()
) -> Question | None:
    """엔진 질문(NextQuestionSchema) + 슬롯 카탈로그 → FE Question.

    보기(options)는 카탈로그 고정 진실 소스. `suggested_answers`(LLM 추천 답변 카드)는
    고정 보기가 없는 자유서술 슬롯에서만 노출한다(chip/select 는 보기로 답하므로 제외).
    """
    nq = state["next_question"]
    slot_key = state["next_slot_key"]
    if nq is None or not slot_key:
        return None
    catalog = CATALOGS[state["kind"]]
    slot = catalog.by_key.get(slot_key)
    options = _question_options(
        slot_key,
        state["slot_answers"],
        kind=state["kind"],
        mandala_goal_titles=mandala_goal_titles,
    )
    return Question(
        slot_key=slot_key,
        text=nq.question,
        answer_type=slot.answer_type if slot else "text",
        options=options,
        suggested_answers=[] if options else list(nq.suggested_answers),
    )


def _response(
    session_id: UUID,
    state: InterviewState,
    *,
    kind: str = "plan",
    end_reason: str | None = None,
    summary: Any = None,
    outcome: Any = None,
    ultimate_outcome: UltimateGoalOutcome | None = None,
    mandala_goal_titles: Sequence[str] = (),
) -> InterviewSession:
    return InterviewSession(
        session_id=str(session_id),
        ambiguity_score=_remaining_required(state["slot_answers"], kind=kind),
        total_turns=state["total_turns"],
        end_reason=end_reason,
        current_question=(
            None
            if end_reason is not None
            else _to_question(state, mandala_goal_titles=mandala_goal_titles)
        ),
        summary=summary,
        outcome=outcome,
        ultimate_outcome=ultimate_outcome,
    )


async def _mandala_goal_titles_if_needed(
    session: AsyncSession, user_id: UUID, slot_key: str | None, *, kind: str
) -> list[str]:
    """`slot_key` 가 `goals.heaviest` 인 plan 세션에서만 DB 를 친다(ADR-0008 §8 "B") —
    다음 질문 조립(`next_slot_key`)과 방금 낸 답 채점(`body.slot_key`) 둘 다 이 조건이면
    호출한다. 그 외 턴은 조회 자체를 안 해 매 턴 쿼리를 붙이지 않는다.
    """
    if kind != "plan" or slot_key != "goals.heaviest":
        return []
    return await mandala_adapter.fetch_promoted_goal_titles_for_user(session, user_id)


def _ended_response(
    row: InterviewSessionRow, slot_rows: list[InterviewSlotAnswer]
) -> InterviewSession:
    """이미 종료된 세션 재조회 — outcome/ultimateOutcome 은 slot_answers 에서 결정적

    재빌드(LLM 0회). `row.kind` 로 분기 — analysis_source 는 영속된 `used_fallback`
    (인터뷰 중 룰 fallback 있었는지) 기준.
    """
    slot_answers = {r.slot_key: r.value for r in slot_rows if r.value is not None}
    ambiguity_final = float(row.ambiguity_final) if row.ambiguity_final is not None else 0.0
    analysis_source: Literal["llm", "rule"] = "rule" if row.used_fallback else "llm"
    outcome = None
    ultimate_outcome = None
    if row.kind == "ultimate":
        ultimate_outcome = ultimate_adapter.build_ultimate_outcome(
            session_id=str(row.id),
            slot_answers=slot_answers,
            ambiguity_final=ambiguity_final,
            end_reason=cast(UltimateEndReason, row.end_reason or "completed"),
            analysis_source=analysis_source,
        )
    else:
        outcome = interview_adapter.build_outcome(
            session_id=str(row.id),
            slot_answers=slot_answers,
            ambiguity_final=ambiguity_final,
            end_reason=cast(InterviewEndReason, row.end_reason or "completed"),
            analysis_source=analysis_source,
        )
    return InterviewSession(
        session_id=str(row.id),
        ambiguity_score=_remaining_required(slot_answers, kind=row.kind),
        total_turns=row.total_turns,
        end_reason=row.end_reason,
        current_question=None,
        summary=None,
        outcome=outcome,
        ultimate_outcome=ultimate_outcome,
    )


async def _persist_turn(
    repo: InterviewRepo, row: InterviewSessionRow, state: InterviewState
) -> None:
    """턴 결과 영속: slot_answers UPSERT + 진행 스칼라 저장."""
    required = CATALOGS[row.kind].required_keys
    for slot_key, value in state["slot_answers"].items():
        await repo.upsert_slot_answer(
            row.id,
            slot_key,
            value,
            is_required=slot_key in required,
        )
    await repo.save_progress(
        row,
        total_turns=state["total_turns"],
        ambiguity_final=state["ambiguity_score"],
        used_fallback=state["used_fallback"],
    )


async def _carry_over_slots(
    repo: InterviewRepo, user_id: UUID, *, source_kind: str, keys: frozenset[str]
) -> dict[str, dict[str, Any]]:
    """`source_kind` 의 가장 최근 '정상 종료' 세션에서 `keys` 슬롯 원답을 회수. 없으면 빈 dict."""
    prev = await repo.get_latest_finished(user_id, kind=source_kind)
    if prev is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in await repo.list_slot_answers(prev.id):
        value = r.value
        if value is not None and r.slot_key in keys and interview_adapter.is_filled_answer(value):
            out[r.slot_key] = value
    return out


async def _carry_over_answers(
    repo: InterviewRepo, profile_repo: ProfileRepo, user: User, *, target_kind: str = "plan"
) -> dict[str, dict[str, Any]]:
    """재인터뷰 시드 — 지난 인터뷰의 지속형 슬롯 원답 위에, **설정에서 수정 가능한 프로필**을
    덮어써 최신 진실을 반영한다(#reduce-reask). 새로 시작하는 인터뷰의 kind 와 무관하게 두
    방향 모두 회수한다(§2.6) — 두 카탈로그의 슬롯키 이름공간(`identity.*`/`goals.*`/... vs
    `ultimate.*`)이 겹치지 않아 병합해도 충돌이 없고, 상대 kind 가 안 쓰는 슬롯은 그 FSM 이
    그냥 읽지 않는다:
    - plan 세션의 `CARRY_OVER_SLOT_KEYS`(자기 자신 — identity·활동창 등, 프로필이 못 담는
      슬롯까지 faithful 하게 회수).
    - ultimate 세션의 `ULTIMATE_CARRY_OVER_SLOT_KEYS`(자기 자신 + 교차 — ultimate.* 는 몇 년에
      한 번 바뀌는 값이라 전량 이월 대상. `goals.list` 같은 **다른** 슬롯은 자동으로 채우지
      않는다 — 그 목표는 사용자가 직접 고르게 한다).

    프로필 오버레이(behavioral/interaction/focus_mode)는 `target_kind="plan"` 일 때만 적용한다
    — 그 프로필들은 계획 인터뷰 슬롯(피크·집중길이·톤·최소단위·휴식수용)에만 매핑돼 있다.
    사용자가 설정에서 고쳤으면 그 값이 이전 인터뷰 원답을 덮는다(최신 우선).

    첫 인터뷰(이력·프로필 없음)면 빈 dict → 기존처럼 전부 묻는다.
    """
    base: dict[str, dict[str, Any]] = {}
    base.update(
        await _carry_over_slots(
            repo, user.id, source_kind="plan", keys=interview_adapter.CARRY_OVER_SLOT_KEYS
        )
    )
    base.update(
        await _carry_over_slots(
            repo,
            user.id,
            source_kind="ultimate",
            keys=ultimate_adapter.ULTIMATE_CARRY_OVER_SLOT_KEYS,
        )
    )

    if target_kind == "plan":
        overlay = profile_memory.seed_slots_from_profile(
            behavioral=await profile_repo.get_behavioral(user.id),
            interaction=await profile_repo.get_interaction(user.id),
            focus_mode_prefs=user.focus_mode_preferences or {},
        )
        base.update(overlay)  # 설정 수정이 반영된 프로필이 지난 인터뷰 원답을 덮는다(최신 우선).
    return base


# ─────────────────────────────────────────────────────────────────────────────
# endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def start_session(
    user: CurrentUser,
    repo: RepoDep,
    profile_repo: ProfileRepoDep,
    session: SessionDep,
    body: StartSessionRequest | None = None,
) -> InterviewSession:
    """딥 인터뷰 세션 시작 — FSM 이 고른 첫 필수 슬롯 질문 1개 생성.

    `body.kind` 기본값이 `"plan"` 이라 본문 없는 기존 호출은 그대로 안전하다(U0b).
    `kind="ultimate"` 로 궁극목표 인터뷰를 시작할 수 있다.

    재시작 승리(restart-wins): 진행 중(end_reason IS NULL) **같은 kind** 세션이 있으면
    `abandoned` 로 닫고 새로 시작한다 — 항상 201. 다른 kind 의 진행 중 인터뷰는 건드리지
    않는다 — 궁극목표 인터뷰 시작이 진행 중인 계획 인터뷰를 죽이면 안 된다. FE 가 sessionId 를
    잃어도(새로고침·localStorage 유실) 재시작만으로 복구된다. 이어하기는 기존
    `next-question` 재개 경로 그대로.
    동시성 lock(ADR-0005 §7.6, kind 스코프) 안에서 검사+생성해 다중 디바이스 race 를 막는다.
    """
    kind = body.kind if body else "plan"
    async with user_agent_lock(session, user.id, _lock_agent(kind)):
        stale = await repo.get_active_session(user.id, kind=kind)
        if stale is not None:
            await repo.finalize(
                stale,
                end_reason="abandoned",
                total_turns=stale.total_turns,
                ambiguity_final=float(stale.ambiguity_final or 0.0),
            )
        seed = await _carry_over_answers(repo, profile_repo, user, target_kind=kind)
        row = await repo.create_session(user.id, get_settings().llm_model, kind=kind)
        result = await interview_runner.start_interview(
            session_id=row.id,
            user_id=user.id,
            kind=kind,
            session=session,
            tone_mode=user.tone_mode,
            slot_meta=_slot_meta(seed, kind=kind),
            seed_answers=seed,
        )
        await _persist_turn(repo, row, result.state)
        await session.commit()
        titles = await _mandala_goal_titles_if_needed(
            session, user.id, result.state.get("next_slot_key"), kind=kind
        )
        return _response(row.id, result.state, kind=kind, mandala_goal_titles=titles)


@router.get("/slot-catalog")
async def get_slot_catalog(
    kind: Literal["plan", "ultimate"] = Query(default="plan"),
) -> list[SlotCatalogEntry]:
    """슬롯 카탈로그 — 클라이언트가 라벨·입력형식·보기(options) 렌더링에 사용.

    `kind` 쿼리 기본값이 `"plan"` 이라 기존 호출(쿼리 없음)은 무변경(U0). 카탈로그 밖 값은
    422(경로 자체가 `Literal` 로 막아 애매한 폴백이 없다).
    """
    return [
        SlotCatalogEntry(
            slot_key=s.slot_key,
            label=s.label,
            answer_type=s.answer_type,
            is_required=s.is_required,
            category=s.category,
            options=list(s.options),
        )
        for s in CATALOGS[kind].slots
    ]


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, user: CurrentUser, repo: RepoDep) -> InterviewSession:
    """인터뷰 진행 상태(모호함 지표). 종료 세션이면 outcome 동봉, 진행 중이면 질문 없음."""
    row = await _load(repo, user.id, session_id)
    slot_rows = await repo.list_slot_answers(row.id)
    if row.end_reason is not None:
        return _ended_response(row, slot_rows)
    slot_answers = {r.slot_key: r.value for r in slot_rows if r.value is not None}
    return InterviewSession(
        session_id=str(row.id),
        ambiguity_score=_remaining_required(slot_answers, kind=row.kind),
        total_turns=row.total_turns,
        end_reason=None,
        current_question=None,
        summary=None,
        outcome=None,
    )


@router.post("/sessions/{session_id}/answers")
async def submit_answer(
    session_id: str,
    body: SlotAnswerRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> InterviewSession:
    """슬롯 답 1개 주입 → 채점/정규화/저장 → 종료면 요약+outcome, 아니면 다음 질문.

    동시성 lock(ADR-0005 §7.6): 다중 디바이스 동시 답 제출로 인한 state race 방지. lock 은
    kind 로 스코프되는데 kind 는 DB 행에만 있어 lock 을 걸기 전엔 알 수 없다 — 그래서 lock
    없이 한 번 살짝 읽어(peek) kind 만 얻고, lock 을 잡은 뒤 **다시 읽어** 그 시점의 최신
    상태로 이후 로직을 돌린다(peek 결과는 kind 외 어떤 판단에도 쓰지 않는다 — 그렇지 않으면
    peek 과 lock 사이의 갱신을 놓치는 race 가 그대로 남는다).
    """
    await endpoint_rate_limit.enforce(session, user_id=user.id, module="interview")
    peek = await _load(repo, user.id, session_id)
    async with user_agent_lock(session, user.id, _lock_agent(peek.kind)):
        row = await _load(repo, user.id, session_id)
        if row.end_reason is not None:
            return _ended_response(row, await repo.list_slot_answers(row.id))

        slot_rows = await repo.list_slot_answers(row.id)
        state = _state_from_db(row, slot_rows)
        answered_slot = CATALOGS[row.kind].by_key.get(body.slot_key)
        answer_titles = await _mandala_goal_titles_if_needed(
            session, user.id, body.slot_key, kind=row.kind
        )
        result = await interview_runner.submit_and_advance(
            state=state,
            slot_key=body.slot_key,
            answer_value=body.value,
            session=session,
            tone_mode=user.tone_mode,
            answer_type=answered_slot.answer_type if answered_slot else None,
            options=_question_options(
                body.slot_key,
                state["slot_answers"],
                kind=row.kind,
                mandala_goal_titles=answer_titles,
            ),
            slot_meta=_slot_meta(state["slot_answers"], kind=row.kind),
        )
        await _persist_turn(repo, row, result.state)

        if result.done:
            reason = result.end_reason or "completed"
            await repo.finalize(
                row,
                end_reason=reason,
                total_turns=result.state["total_turns"],
                ambiguity_final=result.state["ambiguity_score"],
                used_fallback=result.state["used_fallback"],
            )
            # 인터뷰에서 추출한 목표를 즉시 영속(#96) → 목표 분류 화면(GET /goals)이 표시·
            # 재분류할 수 있게 한다. 이후 계획 승인은 같은 목표를 재사용(중복 X).
            # kind="ultimate" 는 result.outcome 이 애초에 None(대신 result.ultimate_outcome)
            # 이라 이 블록이 자연히 스킵된다 — 궁극목표 세션이 직전 계획 인터뷰의 proposed
            # 목표를 supersede_proposed_goals(keep=[]) 로 지워버리는 사고(#186 함정)를
            # "outcome 이 InterviewOutcome 타입일 때만" 이라는 구조 자체가 막는다.
            if result.outcome is not None:
                goal_rows, _ = await first_plan_adapter.materialize_goals(
                    session, user_id=user.id, core_goals=result.outcome.core_goals
                )
                # 지난 인터뷰의 잠정 목표 중 이번에 다시 안 나온 것은 보관 — 세션 restart-wins 를
                # 목표에도 적용해, 계획으로 이어지지 않은 목표가 계속 쌓이지 않게 한다.
                await first_plan_adapter.supersede_proposed_goals(
                    session,
                    user_id=user.id,
                    keep=goal_rows,
                    onboarding_state=user.onboarding_state,
                )
                # 지속형 선호(에너지/톤/시간/회복)를 프로필 메모리에 영속 (#A-1) — 그동안 첫
                # 계획에만 쓰이고 버려지던 Policy Snapshot 레이어를 채운다. 설정에서 편집(#A-2).
                # best-effort: 프로필 영속 실패가 인터뷰 완료를 깨지 않게 (#130 리뷰).
                await _persist_profile_best_effort(session, user=user, outcome=result.outcome)
            await session.commit()
            return _response(
                row.id,
                result.state,
                kind=row.kind,
                end_reason=reason,
                summary=result.summary,
                outcome=result.outcome,
                ultimate_outcome=result.ultimate_outcome,
            )

        await session.commit()
        next_titles = await _mandala_goal_titles_if_needed(
            session, user.id, result.state.get("next_slot_key"), kind=row.kind
        )
        return _response(row.id, result.state, kind=row.kind, mandala_goal_titles=next_titles)


@router.post("/sessions/{session_id}/next-question")
async def next_question(
    session_id: str, user: CurrentUser, repo: RepoDep, session: SessionDep
) -> InterviewSession:
    """현재 미해결 슬롯의 질문 1개 재생성 — 중단된 세션 재개(resume)용.

    동시성 lock(ADR-0005 §7.6): 동시 재개 진입으로 인한 state race 방지. kind 스코프 이유는
    `submit_answer` 와 동일(peek → lock → 재조회).
    """
    await endpoint_rate_limit.enforce(session, user_id=user.id, module="interview")
    peek = await _load(repo, user.id, session_id)
    async with user_agent_lock(session, user.id, _lock_agent(peek.kind)):
        row = await _load(repo, user.id, session_id)
        if row.end_reason is not None:
            return _ended_response(row, await repo.list_slot_answers(row.id))
        slot_rows = await repo.list_slot_answers(row.id)
        state = _state_from_db(row, slot_rows)
        state = await interview.ask_question(
            state, _config(session, _slot_meta(state["slot_answers"], kind=row.kind))
        )
        await _persist_turn(repo, row, state)
        await session.commit()
        titles = await _mandala_goal_titles_if_needed(
            session, user.id, state.get("next_slot_key"), kind=row.kind
        )
        return _response(row.id, state, kind=row.kind, mandala_goal_titles=titles)


@router.post("/sessions/{session_id}/finish")
async def finish_session(
    session_id: str, user: CurrentUser, repo: RepoDep, session: SessionDep
) -> InterviewSession:
    """[충분해요] 조기 종료 — 남은 슬롯은 안전 default 로 채우고 outcome 빌드.

    동시성 lock(ADR-0005 §7.6): 동시 종료/답 제출로 인한 state race 방지. kind 스코프 이유는
    `submit_answer` 와 동일(peek → lock → 재조회).
    """
    peek = await _load(repo, user.id, session_id)
    async with user_agent_lock(session, user.id, _lock_agent(peek.kind)):
        row = await _load(repo, user.id, session_id)
        if row.end_reason is not None:
            return _ended_response(row, await repo.list_slot_answers(row.id))
        slot_rows = await repo.list_slot_answers(row.id)
        state = _state_from_db(row, slot_rows)
        result = await interview_runner.finish_early(
            state=state, session=session, tone_mode=user.tone_mode
        )
        await _persist_turn(repo, row, result.state)
        reason = result.end_reason or "early_user"
        await repo.finalize(
            row,
            end_reason=reason,
            total_turns=result.state["total_turns"],
            ambiguity_final=result.state["ambiguity_score"],
            used_fallback=result.state["used_fallback"],
        )
        # 조기 종료([충분해요])도 완료 경로(submit_answer)와 대칭으로 영속한다 — 순서도 동일.
        # kind="ultimate" 는 result.outcome 이 None(대신 result.ultimate_outcome)이라 자연히
        # 스킵된다 — pitfall #186과 동일 근거(submit_answer 주석 참고).
        if result.outcome is not None:
            # 추출한 목표를 영속(#96). 없으면 [충분해요] 로 끝낸 사용자는 목표 분류 화면이 빈 상태.
            goal_rows, _ = await first_plan_adapter.materialize_goals(
                session, user_id=user.id, core_goals=result.outcome.core_goals
            )
            await first_plan_adapter.supersede_proposed_goals(
                session,
                user_id=user.id,
                keep=goal_rows,
                onboarding_state=user.onboarding_state,
            )
            # 지속형 선호를 프로필 메모리에 영속 (#A-1, best-effort #130).
            await _persist_profile_best_effort(session, user=user, outcome=result.outcome)
        await session.commit()
        return _response(
            row.id,
            result.state,
            kind=row.kind,
            end_reason=reason,
            summary=result.summary,
            outcome=result.outcome,
            ultimate_outcome=result.ultimate_outcome,
        )
