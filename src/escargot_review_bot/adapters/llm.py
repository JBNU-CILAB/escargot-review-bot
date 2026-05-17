import json
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import ollama
from langchain_core.language_models import BaseChatModel
from langchain_ollama import ChatOllama
from langchain_core.runnables import RunnableSerializable

from escargot_review_bot.config.config import (
    MODEL_NAME,
    OLLAMA_BASE_URL,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_MAX_RETRIES,
    OLLAMA_NUM_BATCH,
    OLLAMA_NUM_CTX,
    OLLAMA_REPEAT_PENALTY,
    OLLAMA_TEMPERATURE,
    OLLAMA_TIMEOUT_SECONDS,
    OPENAI_API_KEY,
    OPENAI_MAX_TOKENS,
    INTER_REQUEST_DELAY_SECONDS,
    resolve_pass_model,
    resolve_pass_provider,
)
from escargot_review_bot.config.logging import get_logger


logger = get_logger("review-bot.llm")


_llm_cache: Dict[Tuple[str, str], BaseChatModel] = {}


def _build_chat_model(provider: str, model: str) -> BaseChatModel:
    """Construct a LangChain chat model for the given provider/model.

    Ollama gets its full project parameter set (num_ctx, repeat_penalty, keep_alive);
    OpenAI gets only the cross-provider params (temperature, max_tokens). Other
    Ollama-only knobs are intentionally not mapped to OpenAI to keep the comparison
    honest — see CLAUDE.md §Parameter mapping.
    """
    if provider == "ollama":
        kwargs = dict(
            model=model,
            temperature=OLLAMA_TEMPERATURE,
            num_ctx=OLLAMA_NUM_CTX,
            num_predict=-1,
            repeat_penalty=OLLAMA_REPEAT_PENALTY,
            keep_alive=OLLAMA_KEEP_ALIVE,
        )
        if OLLAMA_BASE_URL:
            kwargs["base_url"] = OLLAMA_BASE_URL
        return ChatOllama(**kwargs)
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        if not OPENAI_API_KEY:
            raise RuntimeError(
                "OPENAI_API_KEY is empty but provider=openai was requested."
            )
        return ChatOpenAI(
            model=model,
            temperature=OLLAMA_TEMPERATURE,
            max_tokens=OPENAI_MAX_TOKENS,
            api_key=OPENAI_API_KEY,
        )
    raise ValueError(f"Unknown LLM provider: {provider!r}")


def get_chat_model(pass_type: str) -> BaseChatModel:
    """Return a cached chat model for the given review pass.

    Provider resolution: `PROVIDER_{PASS}` env > `LLM_PROVIDER` env > "ollama".
    Model resolution:    `MODEL_{PASS}` env > legacy `OLLAMA_MODEL_{PASS}` env.
    """
    provider = resolve_pass_provider(pass_type)
    model = resolve_pass_model(pass_type)
    if not model:
        raise RuntimeError(
            f"No model configured for pass_type={pass_type}. "
            f"Set MODEL_{pass_type.upper()} (or legacy OLLAMA_MODEL_{pass_type.upper()})."
        )
    key = (provider, model)
    if key not in _llm_cache:
        logger.debug(f"Creating chat model provider={provider} model={model}")
        _llm_cache[key] = _build_chat_model(provider, model)
    return _llm_cache[key]


def clear_llm_cache() -> None:
    """Clear the LLM instance cache."""
    _llm_cache.clear()
    logger.debug("LLM cache cleared")


JSON_ARRAY_RE = re.compile(r"\[[\s\S]*?\]")


class OllamaTimeoutError(Exception):
    """Raised when an Ollama API call exceeds the configured timeout."""


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

    Prefer fenced ```json blocks; otherwise scan inline candidates. Returns the
    JSON array string or an empty string when no valid array is found.
    """
    s = raw or ""

    # 1) Code fence first
    m = re.findall(r"```json\s*([\s\S]*?)\s*```", s, flags=re.IGNORECASE)
    for block in m:
        cand = block.strip()
        try:
            obj = json.loads(cand)
            if isinstance(obj, list):
                logger.debug(f"LLM fenced JSON extracted (len={len(cand)})")
                return cand
        except Exception:
            pass

    # 2) If no fence, scan inline candidates and return the first valid JSON array
    candidates = JSON_ARRAY_RE.findall(s)
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, list):
                logger.debug(f"LLM inline JSON array extracted (len={len(cand)})")
                return cand
        except Exception:
            continue

    logger.debug("LLM no JSON array could be extracted from response")
    return ""


def _do_chat_stream(
    use_model: str,
    use_keep_alive: str,
    system_prompt: str,
    user_prompt: str,
) -> List[Dict[str, Any]]:
    """Performs the actual Ollama stream call and JSON parsing (timeout logic is separated).

    This is called in a separate thread by chat_and_parse, a thread-safe timeout wrapper.
    """
    stream = ollama.chat(
        model=use_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        options={
            "temperature": OLLAMA_TEMPERATURE,
            "num_ctx": OLLAMA_NUM_CTX,
            "num_batch": OLLAMA_NUM_BATCH,
            "repeat_penalty": OLLAMA_REPEAT_PENALTY,
        },
        keep_alive=use_keep_alive,
        stream=True,
    )

    buf_parts: List[str] = []
    parsed: Optional[List[Dict[str, Any]]] = None
    for chunk in stream:
        # Normalize chunk content across possible dict/object shapes
        content = getattr(getattr(chunk, "message", None), "content", None)
        if content is None and isinstance(chunk, dict):
            msg = chunk.get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
        if not isinstance(content, str) or not content:
            continue
        # Accumulate partial content and check for a complete JSON array
        buf_parts.append(content)
        text = "".join(buf_parts)
        span = _find_complete_json_array_span(text)
        if span is not None:
            start, end = span
            array_text = text[start:end + 1]
            try:
                raw_comments = json.loads(array_text)
                if isinstance(raw_comments, list):
                    parsed = [c for c in raw_comments if isinstance(c, dict)]
                    if parsed and len(parsed) > 0:
                        logger.debug("LLM stream-early-stop: json array complete")
                        logger.debug(f"LLM items={len(parsed)}")
                    else:
                        logger.debug("LLM stream-early-stop: empty JSON array []")
                    break
            except Exception:
                pass

    if parsed is None:
        if not buf_parts:
            logger.debug("LLM empty response (no content chunks)")
            return []
        # Fallback: sanitize accumulated text to extract a valid array
        text = "".join(buf_parts)
        cleaned = sanitize_llm_output(text)
        if not cleaned:
            logger.debug("LLM sanitize produced empty string; returning []")
            return []
        raw_comments = json.loads(cleaned)
        if not isinstance(raw_comments, list):
            logger.debug(f"LLM parsed non-list JSON: type={type(raw_comments)}")
            return []
        parsed = [c for c in raw_comments if isinstance(c, dict)]
        if not parsed:
            logger.debug("LLM parsed JSON array but contained 0 objects ([])")

    return parsed or []


def chat_and_parse(
    system_prompt: str,
    user_prompt: str,
    model: Optional[str] = None,
    keep_alive: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Stream a chat completion and parse a JSON array from the output.

    model: Ollama model name. If None, uses config MODEL_NAME.
    keep_alive: e.g. "0" (unload after request), "60m". If None, uses OLLAMA_KEEP_ALIVE.

    Thread-safe timeout: uses threading.Timer instead of signal.SIGALRM so that
    multiple passes can run concurrently in a ThreadPoolExecutor without stomping
    on each other's alarm signal.
    """
    use_model = model if model is not None else MODEL_NAME
    use_keep_alive = keep_alive if keep_alive is not None else OLLAMA_KEEP_ALIVE

    for attempt in range(OLLAMA_MAX_RETRIES):
        result_holder: List[Any] = [None]   # [0] = parsed list or exception
        done_event = threading.Event()

        def _worker():
            try:
                result_holder[0] = _do_chat_stream(
                    use_model, use_keep_alive, system_prompt, user_prompt
                )
            except Exception as exc:
                result_holder[0] = exc
            finally:
                done_event.set()

        try:
            logger.info(f"LLM request start model={use_model} keep_alive={use_keep_alive}")
            logger.debug(
                f"LLM attempt {attempt + 1}/{OLLAMA_MAX_RETRIES} "
                f"timeout={OLLAMA_TIMEOUT_SECONDS}s"
            )

            worker_thread = threading.Thread(target=_worker, daemon=True)
            worker_thread.start()

            # Wait up to timeout; if the event fires early we continue immediately
            finished = done_event.wait(timeout=OLLAMA_TIMEOUT_SECONDS)

            if not finished:
                # Thread is still running — treat as timeout
                raise OllamaTimeoutError(
                    f"Ollama API call timed out after {OLLAMA_TIMEOUT_SECONDS}s "
                    f"(model={use_model})"
                )

            outcome = result_holder[0]
            if isinstance(outcome, Exception):
                raise outcome

            parsed = outcome or []
            logger.info(f"LLM parsed comments: count={len(parsed)}")
            try:
                logger.debug(f"LLM sample parsed: {parsed[:2]}")
            except Exception:
                pass

            return parsed

        except OllamaTimeoutError as e:
            logger.warning(f"LLM timeout: {e}")
            if attempt + 1 < OLLAMA_MAX_RETRIES:
                logger.info("LLM retrying...")
            else:
                logger.error("LLM max retries reached. Aborting.")
                return []
        except Exception as e:
            logger.error(f"LLM unexpected error: {e}")
            return []
        finally:
            if INTER_REQUEST_DELAY_SECONDS > 0:
                try:
                    time.sleep(INTER_REQUEST_DELAY_SECONDS)
                except Exception:
                    pass

    return []


# build_review_chain / build_judge_chain were removed when measurement was added.
# Callers (service.py) now invoke prompt → llm → parser directly so they can
# capture AIMessage.usage_metadata and per-call latency before parsing.