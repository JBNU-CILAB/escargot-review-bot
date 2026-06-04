"""LangChain prompt templates for review passes.

This module provides ChatPromptTemplate wrappers around the existing system prompts,
enabling structured prompt management and variable interpolation.
"""

from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from langchain_core.messages import SystemMessage

from escargot_review_bot.prompts.defect import SYSTEM_PROMPT_DEFECT
from escargot_review_bot.prompts.refactor import SYSTEM_PROMPT_REFACTOR
from escargot_review_bot.prompts.compiler import SYSTEM_PROMPT_COMPILER
from escargot_review_bot.prompts.style import SYSTEM_PROMPT_STYLE
from escargot_review_bot.prompts.judge import SYSTEM_PROMPT_JUDGE
from escargot_review_bot.prompts.single import SYSTEM_PROMPT_SINGLE


REVIEW_USER_TEMPLATE = """\
You are a world-class C++ and JavaScript engine reviewer for the Escargot project. Your review must be strict and technically precise.

## Target File: `{file_path}`
## Review Task
Your task is to review the code changes within the `DIFF HUNK` section only. Use the diff purely; do NOT comment on any line outside of the 'Commentable Catalog'.

### Hard Rules
- Choose "target_id" ONLY from **Commentable Catalog (ADDED lines only)**.
- If no qualifying added line has an issue, return [].
- Do NOT mention or infer any line numbers (e.g., "line 47", "at 115"). Anchor only by exact tokens from the chosen line.

---
### 1. DIFF HUNK
```diff
{hunk_text}
```

---
### 2. Commentable Catalog (ADDED lines only; eligible IDs)
```
{commentable_catalog}
```

### Output (JSON array only)
Each object: "target_id", "body", "confidence". If none, return []."""


JUDGE_USER_TEMPLATE = """\
## Target File: `{file_path}`
## Target Line: `{target_code}`

## Proposals:
{proposals_text}"""


defect_prompt = ChatPromptTemplate.from_messages([
    SystemMessage(content=SYSTEM_PROMPT_DEFECT),
    HumanMessagePromptTemplate.from_template(REVIEW_USER_TEMPLATE),
])

refactor_prompt = ChatPromptTemplate.from_messages([
    SystemMessage(content=SYSTEM_PROMPT_REFACTOR),
    HumanMessagePromptTemplate.from_template(REVIEW_USER_TEMPLATE),
])

compiler_prompt = ChatPromptTemplate.from_messages([
    SystemMessage(content=SYSTEM_PROMPT_COMPILER),
    HumanMessagePromptTemplate.from_template(REVIEW_USER_TEMPLATE),
])

style_prompt = ChatPromptTemplate.from_messages([
    SystemMessage(content=SYSTEM_PROMPT_STYLE),
    HumanMessagePromptTemplate.from_template(REVIEW_USER_TEMPLATE),
])

judge_prompt = ChatPromptTemplate.from_messages([
    SystemMessage(content=SYSTEM_PROMPT_JUDGE),
    HumanMessagePromptTemplate.from_template(JUDGE_USER_TEMPLATE),
])

# Single-pass: one combined reviewer (defect+refactor+compiler+style) per hunk,
# no judge merge. Reuses REVIEW_USER_TEMPLATE and the review comment schema.
single_prompt = ChatPromptTemplate.from_messages([
    SystemMessage(content=SYSTEM_PROMPT_SINGLE),
    HumanMessagePromptTemplate.from_template(REVIEW_USER_TEMPLATE),
])


PROMPT_REGISTRY = {
    "defect": defect_prompt,
    "refactor": refactor_prompt,
    "compiler": compiler_prompt,
    "style": style_prompt,
    "judge": judge_prompt,
    "single": single_prompt,
}


def get_prompt(pass_type: str) -> ChatPromptTemplate:
    """Get the ChatPromptTemplate for the specified pass type.
    
    Args:
        pass_type: One of "defect", "refactor", "compiler", "style", "judge"
        
    Returns:
        ChatPromptTemplate configured for the specified pass
        
    Raises:
        KeyError: If pass_type is not recognized
    """
    if pass_type not in PROMPT_REGISTRY:
        raise KeyError(f"Unknown pass type: {pass_type}. Available: {list(PROMPT_REGISTRY.keys())}")
    return PROMPT_REGISTRY[pass_type]
