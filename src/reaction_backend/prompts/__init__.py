"""Prompt Registry (Issue #5 §2).

진입점:
    from reaction_backend.prompts import registry
    body, tmpl = registry.render("interview/next_question", {"goal_title": ...})

DB(`llm_runs.prompt_id` / `llm_runs.prompt_version`) 와 1:1 매핑.
"""

from reaction_backend.prompts import registry
from reaction_backend.prompts.registry import (
    PromptNotFound,
    PromptRenderError,
    PromptTemplate,
)

__all__ = ["PromptNotFound", "PromptRenderError", "PromptTemplate", "registry"]
