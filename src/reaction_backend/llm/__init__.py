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
"""

from reaction_backend.llm.tool_executor import (
    Fallback,
    LLMToolExecutor,
    RunResult,
    aiClient,
)

__all__ = ["Fallback", "LLMToolExecutor", "RunResult", "aiClient"]
