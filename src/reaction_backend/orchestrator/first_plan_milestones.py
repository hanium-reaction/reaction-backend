"""First Plan — 마일스톤 생성 (Stage A, #milestones Phase 2).

목표를 3~5개 중간 목표(마일스톤)로 나눠 사용자에게 확인받는 단계. 세부 세션 분해(Stage B,
`first_plan.decompose_goal`) 전에, 사용자가 계획의 **뼈대**를 수정·확정하게 한다.

- LLM 1콜(`planning/plan_milestones`) + 실패 시 룰 폴백(빈 계획 방지).
- 그래프를 돌지 않는 가벼운 단독 호출 — 라우트(`POST /plans/milestones`)가 직접 부른다.
- 모든 LLM 은 `aiClient.run` 단일 게이트만 (AGENTS §2).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.config import get_settings
from reaction_backend.llm import aiClient
from reaction_backend.orchestrator.first_plan_adapter import context_from_outcome
from reaction_backend.schemas.interview import InterviewOutcome
from reaction_backend.schemas.planning import MilestoneDraft, MilestonePlan


def _rule_milestones(outcome: InterviewOutcome) -> MilestonePlan:
    """LLM 실패 시 룰 폴백 — heaviest 목표를 준비→진행→마무리 3단계로 환원(빈 응답 방지)."""
    goals = outcome.core_goals
    heaviest = next((g for g in goals if g.is_heaviest), goals[0])
    title = heaviest.title
    return MilestonePlan(
        milestones=[
            MilestoneDraft(title=f"{title} 준비·기초", summary="필요한 기초와 준비를 갖춘다"),
            MilestoneDraft(title=f"{title} 핵심 진행", summary="핵심 내용을 실제로 진행한다"),
            MilestoneDraft(
                title=f"{title} 마무리·점검",
                summary=heaviest.success_image or "완료 상태로 마무리한다",
            ),
        ]
    )


async def generate_milestones(
    *,
    outcome: InterviewOutcome,
    density: str = "standard",
    session: AsyncSession | None = None,
    tone_mode: str | None = None,
    user_id: UUID | None = None,
) -> tuple[list[MilestoneDraft], bool]:
    """목표 컨텍스트 → 중간 목표 3~5개. 반환: (마일스톤 목록, 룰 폴백 여부).

    decompose 와 같은 prompt_vars(현재수준·성공이미지·접근·자료 등)를 재사용해 방향을 잡는다.
    """
    settings = get_settings()
    prompt_vars = context_from_outcome(outcome, density=density)["prompt_vars"]
    result = await aiClient.run(
        module="planning",
        schema=MilestonePlan,
        prompt_id="planning/plan_milestones",
        fallback=lambda: _rule_milestones(outcome),
        timeout=settings.llm_planning_timeout_seconds,
        variables=prompt_vars,
        user_id=user_id,
        session=session,
        tone_mode=tone_mode,
        thinking_budget=settings.llm_planning_thinking_budget,
    )
    return list(result.value.milestones), result.fell_back
