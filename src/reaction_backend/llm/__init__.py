"""LLM Tool Executor — 모든 외부 LLM 호출의 단일 게이트 (ADR-0003).

에이전트/오케스트레이터는 이렇게 사용:

    from reaction_backend.llm import aiClient

    result = await aiClient.run(
        module="recovery",
        schema=RecoveryProposal,
        prompt_id="recovery/if_then_proposal",
        fallback=RecoveryProposal(strategy_code="downscope", ...),
        timeout=8.0,
        variables={"failure_type": "...", ...},
        session=session,
        user_id=user.id,
    )

핵심 호출 시그니처는 ADR-0003 §1 으로 **동결**. 변경 시 ADR 갱신 필요.

검색 그라운딩은 반환 계약이 달라(schema 인스턴스가 아니라 원문 텍스트 + 출처) 동결
시그니처를 덮지 않고 **별도 진입점**으로 낸다 (#259 §4.2 ⑥):

    result = await aiClient.run_grounded(
        module="planning",
        prompt_id="planning/materials_search",
        variables={"query": 사용자가_확인한_검색어},
        session=session,
        user_id=user.id,
    )
    if result.text is None:      # 출처 0 건 등 — 자료 없이 진행한다
        ...
"""

from reaction_backend.llm.tool_executor import (
    Fallback,
    GroundedResult,
    GroundingSource,
    LLMToolExecutor,
    RunResult,
    aiClient,
)

__all__ = [
    "Fallback",
    "GroundedResult",
    "GroundingSource",
    "LLMToolExecutor",
    "RunResult",
    "aiClient",
]
