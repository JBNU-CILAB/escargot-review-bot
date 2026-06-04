"""Custom LangChain output parsers for review comment extraction.

The LLM returns JSON arrays of comments, so we need custom parsers that handle
list outputs and provide robust extraction from potentially malformed responses.
"""

import json
import re
from typing import Any, Dict, List, Optional, Type, TypeVar

from langchain_core.output_parsers import BaseOutputParser
from langchain_core.exceptions import OutputParserException
from pydantic import BaseModel, ValidationError

from escargot_review_bot.domain.schemas import LLMReviewComment, JudgeComment
from escargot_review_bot.config.logging import get_logger


logger = get_logger("review-bot.parsers")

T = TypeVar("T", bound=BaseModel)

JSON_ARRAY_RE = re.compile(r"\[[\s\S]*?\]")


def _find_complete_json_array_span(s: str) -> Optional[tuple]:
    """Find the start/end indices of the first complete JSON array in `s`.

    Tracks string escapes and bracket depth so nested arrays are supported.
    Returns (start, end) inclusive indices, or None if no complete array exists.
    """
    if not s:
        return None
    start = s.find("[")
    if start == -1:
        return None
    in_str = False
    esc = False
    depth = 0
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                return (start, i)
    return None


def sanitize_llm_output(raw: str) -> str:
    """Extract the first valid JSON array from raw text.

    Prefer fenced ```json blocks; otherwise extract the first *complete* array
    using bracket-depth + string-escape tracking (so brackets inside a comment
    body — e.g. `[[unlikely]]` or a leading `[CATEGORY]` tag — don't truncate
    the match). Falls back to a naive regex scan only if that fails. Returns the
    JSON array string or an empty string when no valid array is found.
    """
    s = raw or ""

    m = re.findall(r"```json\s*([\s\S]*?)\s*```", s, flags=re.IGNORECASE)
    for block in m:
        cand = block.strip()
        try:
            obj = json.loads(cand)
            if isinstance(obj, list):
                logger.debug(f"Fenced JSON extracted (len={len(cand)})")
                return cand
        except Exception:
            pass

    # Depth/string-aware extraction: correctly skips '[' / ']' that appear
    # inside JSON string values, which the naive regex below cannot.
    span = _find_complete_json_array_span(s)
    if span is not None:
        start, end = span
        cand = s[start:end + 1]
        try:
            if isinstance(json.loads(cand), list):
                logger.debug(f"Span JSON array extracted (len={len(cand)})")
                return cand
        except Exception:
            pass

    # Last-resort fallback for unusual shapes (kept for backward compatibility).
    candidates = JSON_ARRAY_RE.findall(s)
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, list):
                logger.debug(f"Inline JSON array extracted (len={len(cand)})")
                return cand
        except Exception:
            continue

    logger.debug("No JSON array could be extracted from response")
    return ""


class ReviewCommentListParser(BaseOutputParser[List[LLMReviewComment]]):
    """Parser for extracting a list of LLMReviewComment from LLM output.
    
    Handles:
    - JSON arrays wrapped in markdown code fences
    - Raw JSON arrays
    - Partial/malformed JSON with recovery
    """
    
    @property
    def _type(self) -> str:
        return "review_comment_list"
    
    def parse(self, text: str) -> List[LLMReviewComment]:
        """Parse the LLM output into a list of review comments.
        
        Args:
            text: Raw LLM output text
            
        Returns:
            List of validated LLMReviewComment objects
        """
        cleaned = sanitize_llm_output(text)
        if not cleaned:
            logger.debug("Empty output after sanitization, returning []")
            return []
        
        try:
            raw_list = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON decode failed: {e}")
            return []
        
        if not isinstance(raw_list, list):
            logger.warning(f"Expected list, got {type(raw_list)}")
            return []
        
        comments: List[LLMReviewComment] = []
        for item in raw_list:
            if not isinstance(item, dict):
                logger.debug(f"Skipping non-dict item: {type(item)}")
                continue
            try:
                comment = LLMReviewComment(**item)
                comments.append(comment)
            except ValidationError as e:
                logger.debug(f"Validation failed for item {item}: {e}")
                continue
        
        return comments
    
    def get_format_instructions(self) -> str:
        return (
            "Output a JSON array of objects. Each object must have:\n"
            '- "target_id": integer (line ID from the Commentable Catalog)\n'
            '- "body": string (3-8 sentences explaining the issue)\n'
            '- "confidence": float between 0.0 and 1.0\n'
            "If no issues found, return []"
        )


class JudgeCommentListParser(BaseOutputParser[List[JudgeComment]]):
    """Parser for extracting a list of JudgeComment from LLM output.
    
    The Judge pass typically returns 0 or 1 comments, but we use a list
    for consistency with the review passes.
    """
    
    @property
    def _type(self) -> str:
        return "judge_comment_list"
    
    def parse(self, text: str) -> List[JudgeComment]:
        """Parse the LLM output into a list of judge comments.
        
        Args:
            text: Raw LLM output text
            
        Returns:
            List of validated JudgeComment objects (typically 0 or 1)
        """
        cleaned = sanitize_llm_output(text)
        if not cleaned:
            logger.debug("Empty output after sanitization, returning []")
            return []
        
        try:
            raw_list = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON decode failed: {e}")
            return []
        
        if not isinstance(raw_list, list):
            logger.warning(f"Expected list, got {type(raw_list)}")
            return []
        
        comments: List[JudgeComment] = []
        for item in raw_list:
            if not isinstance(item, dict):
                logger.debug(f"Skipping non-dict item: {type(item)}")
                continue
            try:
                comment = JudgeComment(**item)
                comments.append(comment)
            except ValidationError as e:
                logger.debug(f"Validation failed for item {item}: {e}")
                continue
        
        return comments
    
    def get_format_instructions(self) -> str:
        return (
            "Output a JSON array with 0 or 1 objects. Each object must have:\n"
            '- "body": string (integrated review comment)\n'
            '- "confidence": float between 0.0 and 1.0\n'
            "If all proposals should be rejected, return []"
        )


review_comment_list_parser = ReviewCommentListParser()
judge_comment_list_parser = JudgeCommentListParser()
