"""자료 검색 3단계 HITL 흐름 (#259 §5 1차 범위).

    ① POST /plans/materials/search-query   검색어 제안 — 외부 호출 0회 · LLM 0회
    ② POST /plans/materials/search         검색 실행 — 그라운딩 1건, 저장 없음(Draft)
    ③ POST /plans/materials/confirm        사용자 확정 — goals.materials 슬롯에 기록

**③ 이후는 새 배선이 없다.** 확정된 자료는 `goals.materials` 슬롯에 들어가고, 그 뒤는
사용자가 붙여넣은 것과 **완전히 같은 경로**로 흐른다:

    goals.materials → build_outcome → materials_note
                    → materials_for_prompt(울타리 무력화, #260)
                    → planning/goal_decompose

그래서 검색 전용 저장소도, 분해 쪽 분기도 없다(#259 ⑪ "1차는 저장 없음" — 새 테이블·
마이그레이션이 없다는 뜻이고, 사용자가 확정한 자료를 기존 슬롯에 쓰는 건 붙여넣기와 같다).

라이브 실측(2026-08-23)이 이 흐름의 안내 문구를 결정했다: 인프런 강의 커리큘럼은 4/4 로
찾아지지만 상업 교재 목차는 provider 가 저작권으로 막는다(3/4). 그래서 `blocked_copyright`
를 따로 두고 "다시 시도" 가 아니라 "직접 붙여넣어 주세요" 로 안내한다.
"""

from __future__ import annotations

import logging
import re
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.config import get_settings
from reaction_backend.db.models.interview_session import InterviewSession
from reaction_backend.db.session import get_db
from reaction_backend.llm import aiClient
from reaction_backend.llm.tool_executor import GroundedResult
from reaction_backend.orchestrator._common import user_agent_lock
from reaction_backend.orchestrator.interview_adapter import is_placeholder_goal
from reaction_backend.orchestrator.interview_projection import project_session_outcome
from reaction_backend.repositories.interview_repo import InterviewRepo, get_interview_repo
from reaction_backend.safety.llm_budget import check_grounding
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.interview import GoalCandidate, InterviewOutcome
from reaction_backend.schemas.materials import (
    MaterialsConfirmRequest,
    MaterialsConfirmResponse,
    MaterialSource,
    MaterialsQueryRequest,
    MaterialsQueryResponse,
    MaterialsSearchRequest,
    MaterialsSearchResponse,
    MaterialsSearchStatus,
)

_log = logging.getLogger(__name__)

RepoDep = Annotated[InterviewRepo, Depends(get_interview_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]

router = APIRouter(prefix="/plans/materials", tags=["planning"])

_LOCK_AGENT = "materials_search"
_MATERIALS_SLOT = "goals.materials"

# 목표 제목 끝에 붙는 '행위' 표현 — 검색어에서 떼어낸다. "김영한의 실전 자바 완강" 을 그대로
# 검색하면 강의가 아니라 완강 후기가 먼저 잡힌다. 우리가 찾는 건 **자료 자체**다.
_GOAL_VERB_TAIL = re.compile(
    r"(?:완강|완독|정복|마스터|끝내기|끝내자|마치기|떼기|공부하기|시작하기|도전|하기|완료)\s*$"
)


# ─────────────────────────────────────────────────────────────────────────────
# 공통
# ─────────────────────────────────────────────────────────────────────────────


def _no_goal() -> ApiError:
    return ApiError(
        ErrorCode.COMMON_VALIDATION_ERROR,
        "자료를 붙일 목표가 없어요. 인터뷰에서 목표를 먼저 정해 주세요.",
        http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
    )


def _no_interview() -> ApiError:
    return ApiError(
        ErrorCode.COMMON_VALIDATION_ERROR,
        "완료된 인터뷰가 없어요. 인터뷰를 먼저 진행해 주세요.",
        http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
    )


async def _resolve_session(
    repo: InterviewRepo, user_id: UUID, session_id: str | None
) -> InterviewSession:
    """자료를 붙일 인터뷰 세션 — 지정이 없으면 가장 최근 정상 종료 세션."""
    if session_id:
        try:
            parsed = UUID(session_id)
        except ValueError as exc:
            raise _no_interview() from exc
        row = await repo.get_active(user_id, parsed)
        if row is None:
            raise _no_interview()
        return row
    latest = await repo.get_latest_finished(user_id)
    if latest is None:
        raise _no_interview()
    return latest


def _heaviest(outcome: InterviewOutcome) -> GoalCandidate:
    """분해가 실제로 쓰는 목표 — 자료도 이 목표에만 붙는다(`materials_note` 계약).

    placeholder(#88)를 거르고 `is_heaviest` 를 고르는 것은 `first_plan_adapter` 와 **같은
    규칙**이다. 다르면 사용자가 A 목표의 자료를 확정했는데 계획은 B 목표로 짜인다.
    """
    real = [g for g in outcome.core_goals if not is_placeholder_goal(g)]
    if not real:
        raise _no_goal()
    return next((g for g in real if g.is_heaviest), real[0])


def suggest_query(goal: GoalCandidate) -> str:
    """목표에서 검색어를 만든다 — **규칙만. LLM 도 외부 호출도 없다.**

    이 단계가 아무것도 내보내지 않는 것이 ① 프라이버시 결정의 핵심이다. 사용자가 이
    문자열을 보고 고친 뒤에야 2단계에서 외부로 나간다.

    제목 끝의 행위 표현("완강", "끝내기")을 떼는 이유: 그대로 검색하면 자료가 아니라 후기·
    회고가 먼저 잡힌다. 우리가 찾는 건 자료 자체다.
    """
    title = _GOAL_VERB_TAIL.sub("", goal.title.strip()).strip(" -–—·")
    return f"{title} 목차 커리큘럼".strip() if title else "목차 커리큘럼"


# ─────────────────────────────────────────────────────────────────────────────
# ① 검색어 제안
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/search-query")
async def propose_search_query(
    body: MaterialsQueryRequest,
    user: CurrentUser,
    repo: RepoDep,
) -> MaterialsQueryResponse:
    """검색어를 제안한다. **아직 아무것도 검색하지 않는다.**

    외부 호출이 0회라 과금도 예산 차감도 없다 — 사용자가 몇 번을 다시 열어봐도 무료다.
    """
    session_row = await _resolve_session(repo, user.id, body.interview_session_id)
    outcome = await project_session_outcome(session_row, repo)
    goal = _heaviest(outcome)
    return MaterialsQueryResponse(
        suggested_query=suggest_query(goal),
        goal_title=goal.title,
        notice=(
            "이 검색어가 그대로 웹 검색에 쓰여요. 빼고 싶은 내용이 있으면 고쳐 주세요. "
            "고치기 전까지는 아무것도 나가지 않아요."
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# ② 검색 실행
# ─────────────────────────────────────────────────────────────────────────────

# 폐기 사유 → (상태, 사용자에게 할 말). **다음에 할 행동이 다르면 문구도 달라야 한다.**
# 금지어 필터(DevBaseline §4.2)를 통과하는 표현으로 쓴다 — 이 문구들은 우리가 쓴
# 사용자 노출 문자열이다. 회귀는 `test_notices_pass_the_banned_word_filter` 가 잡는다.
_DISCARD_NOTICE: dict[str, tuple[MaterialsSearchStatus, str]] = {
    "ungrounded": (
        "not_found",
        "검색으로는 이 자료의 목차가 확인되지 않았어요. "
        "검색어를 바꿔 보거나, 목차를 직접 붙여넣어 주시면 그대로 반영할게요.",
    ),
    "empty": (
        "not_found",
        "검색 결과에서 읽을 만한 내용을 찾지 않았어요. "
        "목차를 직접 붙여넣어 주시면 그대로 반영할게요.",
    ),
    "recitation": (
        "blocked_copyright",
        "이 자료는 저작권 보호 때문에 검색으로 가져올 수 없어요. "
        "다시 시도해도 같으니, 목차를 직접 붙여넣어 주시면 그대로 반영할게요.",
    ),
    "grounding_budget": (
        "quota_exceeded",
        "오늘 쓸 수 있는 자료 검색을 다 썼어요. 내일 다시 쓸 수 있고, "
        "목차를 직접 붙여넣으면 지금 바로 반영돼요.",
    ),
    "budget": (
        "quota_exceeded",
        "오늘 쓸 수 있는 AI 사용량을 다 썼어요. 내일 다시 쓸 수 있고, "
        "목차를 직접 붙여넣으면 지금 바로 반영돼요.",
    ),
}
_UNAVAILABLE_NOTICE = (
    "지금은 검색이 잘 되지 않네요. 잠시 후 다시 시도하거나, "
    "목차를 직접 붙여넣어 주시면 그대로 반영할게요."
)
_FOUND_NOTICE = "이 자료를 참고했어요. 내용이 맞는지 확인하고, 다르면 고친 뒤에 확정해 주세요."


def _to_response(result: GroundedResult, *, remaining: int | None) -> MaterialsSearchResponse:
    """`GroundedResult` → API 응답. 폐기 사유를 사용자 행동으로 번역하는 곳."""
    sources = [MaterialSource(title=s.title, uri=s.uri) for s in result.sources]
    if result.usable:
        return MaterialsSearchResponse(
            status="found",
            text=result.text,
            sources=sources,
            search_queries=list(result.search_queries),
            notice=_FOUND_NOTICE,
            remaining_today=remaining,
            ai_source="llm",
        )
    status, notice = _DISCARD_NOTICE.get(result.reason or "", ("unavailable", _UNAVAILABLE_NOTICE))
    return MaterialsSearchResponse(
        status=status,
        text=None,
        sources=sources,
        search_queries=list(result.search_queries),
        notice=notice,
        remaining_today=remaining,
        ai_source="llm",
    )


@router.post("/search")
async def search_materials(
    body: MaterialsSearchRequest,
    user: CurrentUser,
    session: SessionDep,
) -> MaterialsSearchResponse:
    """사용자가 확정한 검색어로 자료를 찾는다. 결과는 **Draft — 저장하지 않는다.**

    동시 요청을 락으로 직렬화하는 이유는 성능이 아니라 **예산**이다. 그라운딩은 건당
    과금인데, 두 요청이 나란히 들어오면 둘 다 잔량 검사를 통과한 뒤 둘 다 호출해
    상한을 넘길 수 있다(TOCTOU). 락 안에서 검사→호출→기록이 한 트랜잭션으로 끝난다.
    """
    async with user_agent_lock(session, user.id, _LOCK_AGENT):
        result = await aiClient.run_grounded(
            "planning",
            "planning/materials_search",
            variables={"query": body.query},
            user_id=user.id,
            session=session,
        )
        remaining = await _remaining_today(session, user.id)
        await session.commit()

    _log.info(
        "materials_search",
        extra={
            "user_id": str(user.id),
            "reason": result.reason,
            "sources": len(result.sources),
            "grounding_requests": result.grounding_requests,
        },
    )
    return _to_response(result, remaining=remaining)


async def _remaining_today(session: SessionDep, user_id: UUID) -> int | None:
    """오늘 남은 검색 횟수. 한도 0(무제한)이면 None — 화면에 "남은 횟수"를 띄우지 않게."""
    if get_settings().llm_daily_grounding_budget <= 0:
        return None
    try:
        status = await check_grounding(session, user_id=user_id, projected_requests=0)
    except Exception:  # noqa: BLE001 - 잔량 표시는 부가 정보라 실패해도 본 응답을 막지 않는다
        return None
    return max(0, status.limit - status.used)


# ─────────────────────────────────────────────────────────────────────────────
# ③ 사용자 확정
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/confirm")
async def confirm_materials(
    body: MaterialsConfirmRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> MaterialsConfirmResponse:
    """ "이 자료 맞아요" — 확정본을 `goals.materials` 슬롯에 쓴다 (② HITL 결정).

    여기서부터는 **붙여넣기와 구분되지 않는다.** 다음 계획 생성이 기존 경로로 이 값을
    집어가고, 자료 텍스트는 `materials_for_prompt` 의 울타리 안에 들어간다(#260).

    사용자가 고친 텍스트를 그대로 받는다 — 검색이 다른 판을 가져왔거나 일부만 맞을 때
    지우고 붙일 수 있어야 HITL 이다.
    """
    session_row = await _resolve_session(repo, user.id, body.interview_session_id)
    outcome = await project_session_outcome(session_row, repo)
    goal = _heaviest(outcome)

    text = body.text.strip()
    await repo.upsert_slot_answer(
        session_row.id,
        _MATERIALS_SLOT,
        {"type": "text", "raw": text},
        is_required=True,
    )
    await session.commit()

    return MaterialsConfirmResponse(
        goal_title=goal.title,
        saved_chars=len(text),
        notice="자료를 반영했어요. 다음 계획 생성부터 이 목차를 뼈대로 삼아요.",
    )
