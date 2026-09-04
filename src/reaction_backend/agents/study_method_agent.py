"""Worker Agent — 목표 → 추천 학습 방식 + 자료 검색어 2종 (ADR-0010 파이프라인 1단계).

세션 소유권 규약은 `ultimate_summary_agent.py` 와 동일 — `session` 은 `aiClient.run` 전달
외에 쓰지 않고, 반환은 항상 `(값, fell_back)`.

그라운딩을 쓰지 않는 이유는 ADR-0010 §2 — 이 산출물은 저작물 인용이 아니라 일반적 학습
전략 조언이라 RECITATION 리스크가 없고, `goal_decompose`·`mandala_cells` 와 같은 결의
구조화 호출 1회로 충분하다.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.config import get_settings
from reaction_backend.llm import aiClient
from reaction_backend.schemas.interview import GoalCandidate
from reaction_backend.schemas.study_method import StudyMethodPlan

_NOT_ANSWERED = "(응답 없음)"
_NOT_GIVEN = "(없음)"


def _rule_plan(goal: GoalCandidate) -> StudyMethodPlan:
    """LLM 실패 시 폴백 — 목표 제목에서 결정적으로 만든 최소 질의.

    `api/routes/materials.py::suggest_query` 와 같은 접미어("목차 커리큘럼")를 도서
    질의에 쓴다 — 룰 경로가 기존 자료 검색 흐름과 갈라지지 않게 맞춘다.
    """
    title = goal.title.strip() or "목표"
    return StudyMethodPlan(
        approach=f"'{title}' 에 맞는 자료를 찾아 계획에 반영해요.",
        focus_points=[],
        book_query=f"{title} 목차 커리큘럼",
        video_query=f"{title} 강의",
        # 판단 근거가 없을 때는 좁히지 않는다 — 사용자가 어차피 최종 선택을 하므로
        # "both" 로 둬도 선택지가 줄지 않는다(schema 기본값과 같은 값, 명시적으로 남긴다).
        material_mix="both",
    )


async def run(
    *,
    goal: GoalCandidate,
    session: AsyncSession | None,
    user_id: UUID,
    tone_mode: str | None = None,
) -> tuple[StudyMethodPlan, bool]:
    """목표 → 추천 방식 + 도서/영상 검색어. 반환: (계획, fell_back)."""
    settings = get_settings()
    variables = {
        "title": goal.title,
        "category": goal.category,
        "current_level": goal.current_level or _NOT_ANSWERED,
        "weekly_hours": f"{goal.weekly_hours}시간"
        if goal.weekly_hours is not None
        else _NOT_ANSWERED,
        "session_length_min": (
            f"{goal.session_length_min}분" if goal.session_length_min is not None else _NOT_ANSWERED
        ),
        "approach_note": goal.approach_note or _NOT_GIVEN,
        "deadline": goal.deadline or _NOT_GIVEN,
    }
    result = await aiClient.run(
        module="planning",
        schema=StudyMethodPlan,
        prompt_id="planning/study_method",
        fallback=lambda: _rule_plan(goal),
        timeout=settings.llm_timeout_seconds,
        variables=variables,
        user_id=user_id,
        session=session,
        tone_mode=tone_mode,
    )
    return result.value, result.fell_back


__all__ = ["run"]
