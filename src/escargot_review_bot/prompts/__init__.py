"""Prompt definitions and templates for review passes."""

from escargot_review_bot.prompts.defect import SYSTEM_PROMPT_DEFECT
from escargot_review_bot.prompts.refactor import SYSTEM_PROMPT_REFACTOR
from escargot_review_bot.prompts.compiler import SYSTEM_PROMPT_COMPILER
from escargot_review_bot.prompts.style import SYSTEM_PROMPT_STYLE
from escargot_review_bot.prompts.judge import SYSTEM_PROMPT_JUDGE
from escargot_review_bot.prompts.single import SYSTEM_PROMPT_SINGLE
from escargot_review_bot.prompts.templates import (
    get_prompt,
    defect_prompt,
    refactor_prompt,
    compiler_prompt,
    style_prompt,
    judge_prompt,
    single_prompt,
    PROMPT_REGISTRY,
)

__all__ = [
    "SYSTEM_PROMPT_DEFECT",
    "SYSTEM_PROMPT_REFACTOR",
    "SYSTEM_PROMPT_COMPILER",
    "SYSTEM_PROMPT_STYLE",
    "SYSTEM_PROMPT_JUDGE",
    "SYSTEM_PROMPT_SINGLE",
    "get_prompt",
    "defect_prompt",
    "refactor_prompt",
    "compiler_prompt",
    "style_prompt",
    "judge_prompt",
    "single_prompt",
    "PROMPT_REGISTRY",
]
