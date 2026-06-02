import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import langsmith as ls
from fastapi import HTTPException
from unidiff import PatchSet, Hunk

from escargot_review_bot.adapters.git import run_git_command
from escargot_review_bot.adapters.llm import get_chat_model
from escargot_review_bot.adapters.metrics import (
    MetricsLogger,
    NullMetricsLogger,
    extract_usage,
    new_skip_reason_counter,
)
from escargot_review_bot.adapters.parsers import (
    judge_comment_list_parser,
    review_comment_list_parser,
)
from escargot_review_bot.config.config import (
    ALIGN_SEARCH_WINDOW,
    CONFIDENCE_THRESHOLD,
    DIFF_CONTEXT,
    EXPERIMENT_LOG_DIR,
    EXPERIMENT_LOGGING_ENABLED,
    OLLAMA_KEEP_ALIVE,
    PASS_TYPES,
    REVIEW_INCLUDE_PATHS,
    REVIEW_PARALLEL_PASSES,
    REVIEW_PARALLEL_WORKERS,
    any_pass_uses_provider,
    resolve_pass_model,
    resolve_pass_provider,
)
from escargot_review_bot.config.logging import get_logger
from escargot_review_bot.domain.schemas import (
    GitHubComment,
    LLMReviewComment,
    ReviewRequest,
)
from escargot_review_bot.prompts import get_prompt


MetricsLike = Union[MetricsLogger, NullMetricsLogger]


logger = get_logger("review-bot.service")

# Pass-type → comment tag mapping
PASS_TAG: dict = {"defect": "[D]", "refactor": "[R]", "compiler": "[C]", "style": "[S]"}


class LineMappingLite:
    """Unified diff line mapping with stable `target_id` and side line numbers."""
    def __init__(self, target_id: int, line_type: str, content: str,
                 source_line_no: Optional[int], target_line_no: Optional[int]) -> None:
        self.target_id = target_id
        self.line_type = line_type
        self.content = content
        self.source_line_no = source_line_no
        self.target_line_no = target_line_no


def create_line_mappings_for_hunk(hunk: Hunk) -> List[LineMappingLite]:
    """Build `LineMappingLite` list from a hunk with stable IDs and positions.

    Assigns a monotonic `target_id` to each hunk line and records source/target
    side line numbers alongside raw content for later anchoring.
    """
    # Build stable ID -> line mapping while tracking left/right cursors
    mappings: List[LineMappingLite] = []
    current_id = 1

    right_line = hunk.target_start
    left_line = hunk.source_start

    for line in hunk:
        if line.is_added:
            # Added lines exist only on the right (target) side
            mappings.append(LineMappingLite(
                target_id=current_id,
                line_type='added',
                content=line.value,
                source_line_no=None,
                target_line_no=right_line,
            ))
            right_line += 1
        elif line.is_removed:
            # Removed lines exist only on the left (source) side
            mappings.append(LineMappingLite(
                target_id=current_id,
                line_type='removed',
                content=line.value,
                source_line_no=left_line,
                target_line_no=None,
            ))
            left_line += 1
        else:
            # Context lines exist on both sides and advance both cursors
            mappings.append(LineMappingLite(
                target_id=current_id,
                line_type='context',
                content=line.value,
                source_line_no=left_line,
                target_line_no=right_line,
            ))
            left_line += 1
            right_line += 1
        current_id += 1
    return mappings


def normalize_for_compare(s: str) -> str:
    """Expand tabs(4) and strip to normalize for alignment comparison."""
    return (s or "").expandtabs(4).strip()


def line_without_prefix(raw: str) -> str:
    """Remove leading '+'/'-' from diff line; return empty string if falsy."""
    if not raw:
        return ""
    if raw[0] in {"+", "-"}:
        return raw[1:]
    return raw


def is_meaningful_code(raw: str) -> bool:
    """Heuristic: exclude empty/brace-only lines from review candidates."""
    s = (raw or "").strip()
    if not s:
        return False
    if s in {'{', '}', '};'}:
        return False
    return True


def _collect_target_side_context(
    mappings: List[LineMappingLite],
    center_index: int,
    max_depth: int = 2,
) -> Tuple[List[str], List[str]]:
    """Collect up to `max_depth` normalized target-side neighbor lines.

    Returns (prev_list, next_list), where each element is normalized with
    `line_without_prefix` and `normalize_for_compare`. Only mappings with a
    valid `target_line_no` are considered.
    """
    prev_ctx: List[str] = []
    next_ctx: List[str] = []

    # Walk left for previous target-side lines
    i = center_index - 1
    while i >= 0 and len(prev_ctx) < max_depth:
        mi = mappings[i]
        if mi.target_line_no is not None:
            prev_ctx.append(normalize_for_compare(line_without_prefix(mi.content)))
        i -= 1

    # Walk right for next target-side lines
    i = center_index + 1
    while i < len(mappings) and len(next_ctx) < max_depth:
        mi = mappings[i]
        if mi.target_line_no is not None:
            next_ctx.append(normalize_for_compare(line_without_prefix(mi.content)))
        i += 1

    return prev_ctx, next_ctx


def assert_head_alignment(head_sha: str, path: str, mapping: LineMappingLite,
                          head_cache: Dict[str, List[str]]) -> Optional[bool]:
    """Check exact alignment of an added line at `target_line_no` in HEAD.

    Uses a cached `git show {head_sha}:{path}` blob. Returns True/False for
    match/mismatch, or None if not applicable (non-added or missing position).
    """
    if mapping.line_type != 'added' or mapping.target_line_no is None:
        return None

    # Lazy-load and cache the HEAD blob lines for this file
    key = f"{head_sha}:{path}"
    if key not in head_cache:
        blob_text = run_git_command(["show", f"{head_sha}:{path}"])
        head_cache[key] = blob_text.splitlines()

    lines = head_cache[key]
    idx = mapping.target_line_no - 1
    # Treat out-of-range as invalid expected position against HEAD
    if not (0 <= idx < len(lines)):
        logger.debug(f"Align out-of-range: {path}:{mapping.target_line_no} (len={len(lines)})")
        return False

    # Normalize both sides before equality check to avoid whitespace noise
    expected = normalize_for_compare(line_without_prefix(mapping.content))
    actual = normalize_for_compare(lines[idx])

    if expected == actual:
        return True

    logger.debug(
        f"Align mismatch: {path}:{mapping.target_line_no} expected={expected!r} actual={actual!r}"
    )
    return False


def try_nearby_align(
    head_sha: str,
    path: str,
    mapping: LineMappingLite,
    head_cache: Dict[str, List[str]],
    prev_context: Optional[List[str]] = None,
    next_context: Optional[List[str]] = None,
) -> Optional[int]:
    """Search within +/-`ALIGN_SEARCH_WINDOW` for a nearby normalized match.

    If multiple candidates are found, disambiguate using up to 1-2 lines of
    previous/next target-side context. Only a unique highest-scoring candidate
    is accepted; otherwise return None.
    """
    if mapping.line_type != 'added' or mapping.target_line_no is None:
        return None

    # Reuse cached HEAD blob if already loaded; otherwise load once
    key = f"{head_sha}:{path}"
    if key not in head_cache:
        blob_text = run_git_command(["show", f"{head_sha}:{path}"])
        head_cache[key] = blob_text.splitlines()

    lines = head_cache[key]
    total = len(lines)
    base_idx = mapping.target_line_no - 1
    expected = normalize_for_compare(line_without_prefix(mapping.content))

    # Quick path: current index already matches after normalization
    if 0 <= base_idx < total and normalize_for_compare(lines[base_idx]) == expected:
        return mapping.target_line_no

    # Collect all candidate positions within the search window
    candidates: List[int] = []
    for delta in range(1, ALIGN_SEARCH_WINDOW + 1):
        up = base_idx - delta
        if 0 <= up < total and normalize_for_compare(lines[up]) == expected:
            candidates.append(up)
        down = base_idx + delta
        if 0 <= down < total and normalize_for_compare(lines[down]) == expected:
            candidates.append(down)

    if not candidates:
        return None

    # If single candidate, accept it
    if len(candidates) == 1:
        return candidates[0] + 1

    # Disambiguate using neighbor context if provided
    prev_context = prev_context or []
    next_context = next_context or []

    best_idx: Optional[int] = None
    best_score = -1
    tie = False

    for pos in candidates:
        score = 0
        # Match previous neighbors: prev_context[0] is nearest neighbor
        for offset, txt in enumerate(prev_context, start=1):
            nei = pos - offset
            if 0 <= nei < total and normalize_for_compare(lines[nei]) == txt:
                score += 1
        # Match next neighbors: next_context[0] is nearest neighbor
        for offset, txt in enumerate(next_context, start=1):
            nei = pos + offset
            if 0 <= nei < total and normalize_for_compare(lines[nei]) == txt:
                score += 1

        if score > best_score:
            best_score = score
            best_idx = pos
            tie = False
        elif score == best_score:
            tie = True

    # Accept only a unique highest-scoring candidate with some context support
    if best_idx is not None and not tie and best_score > 0:
        return best_idx + 1

    return None


def prepare_chain_input(path: str, hunk: Hunk, mappings: List[LineMappingLite]) -> Dict[str, str]:
    """Prepare input dictionary for LangChain review chains.

    Returns a dict with keys: file_path, hunk_text, commentable_catalog
    that can be passed directly to build_review_chain().invoke()
    """
    hunk_text = str(hunk)
    commentable = [
        m for m in mappings
        if m.line_type == 'added' and is_meaningful_code(line_without_prefix(m.content))
    ]
    if commentable:
        commentable_catalog = [
            f"<ID {m.target_id} | {m.line_type.upper()}>: {line_without_prefix(m.content).strip()}"
            for m in commentable
        ]
        commentable_str = "\n".join(commentable_catalog)
    else:
        commentable_str = "(no added lines)"

    return {
        "file_path": path,
        "hunk_text": hunk_text,
        "commentable_catalog": commentable_str,
    }


def fetch_upstream_with_fallback(pull_request_number: int, base_sha: str, head_sha: str) -> None:
    """Ensure PR refs/SHAs exist locally with pragmatic fallback fetches.

    Prunes/fetches upstream, tries PR ref, then validates and fetches SHAs
    directly if missing; raises HTTPException on final absence.
    """
    # 1) prune and fetch upstream to refresh remote refs
    try:
        logger.debug("Upstream prune fetch start")
        run_git_command(["fetch", "upstream", "--prune"])
        logger.debug("Upstream prune fetch done")
    except Exception as e:
        logger.warning(f"Upstream prune fetch failed (continuing): {e}")

    # 2) attempt to fetch PR head ref; fall back to raw SHAs if missing
    try:
        run_git_command(["fetch", "upstream", f"refs/pull/{pull_request_number}/head"])
    except Exception as e:
        logger.warning(f"PR ref not found (continuing with SHAs): {e}")

    # 3) ensure both base/head SHAs are present; try direct SHA fetch when absent
    for sha in [base_sha, head_sha]:
        try:
            run_git_command(["cat-file", "-e", f"{sha}^{{commit}}"])
            continue
        except Exception:
            pass
        try:
            run_git_command(["fetch", "upstream", sha])
        except Exception as e:
            logger.warning(f"Direct SHA fetch failed (sha={sha}): {e}")
        try:
            run_git_command(["cat-file", "-e", f"{sha}^{{commit}}"])
        except Exception:
            logger.error(f"Missing commit after fetch attempts: {sha}")
            raise HTTPException(status_code=400, detail=f"Missing commit in upstream: {sha}")


def _run_review_pass(
    model_type: str,
    file_path: str,
    hunk: Hunk,
    mappings: List[LineMappingLite],
    mapping_dict: Dict[int, Any],
    head_sha: str,
    head_blob_cache: Dict[str, List[str]],
    metrics: MetricsLike,
    hunk_id: str,
) -> List[Dict[str, Any]]:
    """Run one pass (defect/refactor/compiler/style).

    Inlines prompt → llm → parser (instead of using a wrapped LCEL chain) so we
    can read AIMessage.usage_metadata and time the LLM call itself for paper-
    grade measurement. Parent hunk-level LangSmith trace is still active.
    """
    chain_input = prepare_chain_input(file_path, hunk, mappings)
    provider = resolve_pass_provider(model_type)
    model_name = resolve_pass_model(model_type)
    logger.debug(f"{model_type.title()} pass: provider={provider} model={model_name}")

    prompt = get_prompt(model_type)
    try:
        llm = get_chat_model(model_type)
    except Exception as e:
        logger.error(f"{model_type} pass: chat model init failed: {e}")
        metrics.log_llm_call(
            pass_type=model_type, provider=provider, model=model_name,
            hunk_id=hunk_id, latency_ms=0, usage={},
            raw_comments_count=0, error=f"init: {e}",
        )
        return []

    start = time.perf_counter()
    try:
        prompt_value = prompt.invoke(chain_input)
        ai_message = llm.invoke(prompt_value, config={"run_name": f"ChatOpenAI_{model_type.capitalize()}_Pass"})
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.error(f"{model_type} pass: LLM invoke failed: {e}")
        metrics.log_llm_call(
            pass_type=model_type, provider=provider, model=model_name,
            hunk_id=hunk_id, latency_ms=latency_ms, usage={},
            raw_comments_count=0, error=str(e),
        )
        return []
    latency_ms = int((time.perf_counter() - start) * 1000)
    usage = extract_usage(ai_message)

    try:
        comments = review_comment_list_parser.invoke(ai_message)
    except Exception as e:
        logger.error(f"{model_type} pass: parser failed: {e}")
        metrics.log_llm_call(
            pass_type=model_type, provider=provider, model=model_name,
            hunk_id=hunk_id, latency_ms=latency_ms, usage=usage,
            raw_comments_count=0, error=f"parse: {e}",
        )
        return []

    logger.info(f"{model_type} pass: LLM returned {len(comments)} raw comment(s)")
    metrics.log_llm_call(
        pass_type=model_type, provider=provider, model=model_name,
        hunk_id=hunk_id, latency_ms=latency_ms, usage=usage,
        raw_comments_count=len(comments),
    )

    out_comments: List[Dict[str, Any]] = []
    accepted: Set[int] = set()
    skips = new_skip_reason_counter()

    for llm_comment in comments:
        if llm_comment.target_id in accepted:
            logger.debug(f"Skip({model_type}): duplicate target_id={llm_comment.target_id} in this pass")
            skips["duplicate_in_pass"] += 1
            continue

        if llm_comment.confidence < CONFIDENCE_THRESHOLD:
            logger.debug(f"Skip({model_type}): low confidence {llm_comment.confidence:.2f} < {CONFIDENCE_THRESHOLD}")
            skips["low_confidence"] += 1
            continue

        m = mapping_dict.get(llm_comment.target_id)
        if not m or m.line_type != 'added' or m.target_line_no is None:
            logger.debug(f"Skip({model_type}): invalid target_id={llm_comment.target_id} or not added line")
            skips["invalid_target_id"] += 1
            continue

        line_no = m.target_line_no
        head_ok = assert_head_alignment(head_sha, file_path, m, head_blob_cache)
        if head_ok is False:
            logger.debug(f"Align mismatch at ~{line_no}, trying nearby align...")
            try:
                center_index = next(i for i, mm in enumerate(mappings) if mm.target_id == m.target_id)
            except StopIteration:
                center_index = None
            prev_ctx: List[str] = []
            next_ctx: List[str] = []
            if center_index is not None:
                prev_ctx, next_ctx = _collect_target_side_context(mappings, center_index, max_depth=2)

            aligned = try_nearby_align(
                head_sha,
                file_path,
                m,
                head_blob_cache,
                prev_context=prev_ctx,
                next_context=next_ctx,
            )
            if aligned is None:
                logger.debug(f"Skip({model_type}): nearby align failed")
                skips["align_failed"] += 1
                continue
            line_no = aligned

        tag = PASS_TAG.get(model_type, "")
        final_comment = GitHubComment(
            path=file_path,
            body=f"{tag} {llm_comment.body}" if tag else llm_comment.body,
            commit_id=head_sha,
            line=line_no,
            side="RIGHT"
        )
        out_comments.append(final_comment.model_dump())
        accepted.add(llm_comment.target_id)
        logger.debug(f"Accept({model_type}): id={llm_comment.target_id} -> line={line_no}")

    if len(comments) > 0 and len(out_comments) == 0:
        logger.warning(
            f"{model_type} pass: all {len(comments)} comment(s) dropped by filters "
            "(confidence/target_id/HEAD alignment). Check LOG_LEVEL=DEBUG for Skip reasons."
        )

    metrics.log_pass_summary(
        pass_type=model_type, hunk_id=hunk_id,
        raw_comments=len(comments), accepted=len(out_comments),
        skip_reasons=skips,
    )
    return out_comments


_PASS_ORDER = ("defect", "refactor", "compiler", "style")

def _merge_comments_by_line(
    hunk_item: Tuple[str, Hunk, List[LineMappingLite], Dict[int, Any]],
    defect_comments: List[Dict[str, Any]],
    refactor_comments: List[Dict[str, Any]],
    compiler_comments: List[Dict[str, Any]],
    style_comments: List[Dict[str, Any]],
    metrics: MetricsLike,
    hunk_id: str,
) -> List[Dict[str, Any]]:
    """Group by (path, line) and use the Judge pass to merge proposals.

    Each Judge round-trip is measured (latency + usage) via `metrics.log_judge_call`.
    """
    fp, h, m, mapping_dict = hunk_item
    key_to_bodies: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for label, comments in [
        ("defect", defect_comments),
        ("refactor", refactor_comments),
        ("compiler", compiler_comments),
        ("style", style_comments),
    ]:
        for c in comments:
            path = c.get("path", "")
            line = c.get("line")
            if line is None:
                continue
            key = (path, line)
            if key not in key_to_bodies:
                key_to_bodies[key] = {"path": path, "line": line, "commit_id": c.get("commit_id"), "side": c.get("side", "RIGHT"), "parts": []}
            body = (c.get("body") or "").strip()
            if body:
                key_to_bodies[key]["parts"].append((label, body))

    merged: List[Dict[str, Any]] = []
    judge_provider = resolve_pass_provider("judge")
    judge_model = resolve_pass_model("judge")
    judge_prompt = get_prompt("judge")
    try:
        judge_llm = get_chat_model("judge")
    except Exception as e:
        logger.error(f"Judge chat model init failed: {e}")
        return merged

    for key in sorted(key_to_bodies.keys()):
        info = key_to_bodies[key]
        parts = info["parts"]
        if not parts:
            continue

        target_code = "(Failed to map the line)"
        for mapping in m:
            if mapping.target_line_no == info["line"]:
                target_code = line_without_prefix(mapping.content).strip()
                break

        order_idx = {p: i for i, p in enumerate(_PASS_ORDER)}
        parts_sorted = sorted(parts, key=lambda x: order_idx.get(x[0], 99))
        proposals_passes = [p for p, _ in parts_sorted]

        proposals_text = ""
        for p_label, b_text in parts_sorted:
            proposals_text += f"[{p_label.upper()}]\n{b_text}\n\n"

        logger.debug(f"Judge pass starting for {info['path']}:{info['line']} (proposals: {len(parts)})")

        start = time.perf_counter()
        try:
            prompt_value = judge_prompt.invoke({
                "file_path": info["path"],
                "target_code": target_code,
                "proposals_text": proposals_text.strip(),
            })
            ai_message = judge_llm.invoke(prompt_value, config={"run_name": "ChatOpenAI_Judge_Pass"})
        except Exception as e:
            latency_ms = int((time.perf_counter() - start) * 1000)
            logger.error(f"Judge invoke failed: {e}")
            metrics.log_judge_call(
                provider=judge_provider, model=judge_model,
                hunk_id=hunk_id, line=info["line"],
                proposals_count=len(parts), proposals_passes=proposals_passes,
                merged_count=0, latency_ms=latency_ms, usage={}, error=str(e),
            )
            continue
        latency_ms = int((time.perf_counter() - start) * 1000)
        usage = extract_usage(ai_message)

        try:
            judge_comments = judge_comment_list_parser.invoke(ai_message)
        except Exception as e:
            logger.error(f"Judge parser failed: {e}")
            metrics.log_judge_call(
                provider=judge_provider, model=judge_model,
                hunk_id=hunk_id, line=info["line"],
                proposals_count=len(parts), proposals_passes=proposals_passes,
                merged_count=0, latency_ms=latency_ms, usage=usage,
                error=f"parse: {e}",
            )
            continue

        if not judge_comments:
            logger.debug(f"Judge pass rejected proposals for {info['line']}.")
            metrics.log_judge_call(
                provider=judge_provider, model=judge_model,
                hunk_id=hunk_id, line=info["line"],
                proposals_count=len(parts), proposals_passes=proposals_passes,
                merged_count=0, latency_ms=latency_ms, usage=usage,
            )
            continue

        judged_comment = judge_comments[0].body.strip()
        if not judged_comment:
            metrics.log_judge_call(
                provider=judge_provider, model=judge_model,
                hunk_id=hunk_id, line=info["line"],
                proposals_count=len(parts), proposals_passes=proposals_passes,
                merged_count=0, latency_ms=latency_ms, usage=usage,
            )
            continue

        merged.append({
            "path": info["path"],
            "body": judged_comment,
            "commit_id": info["commit_id"],
            "line": info["line"],
            "side": info["side"],
        })
        metrics.log_judge_call(
            provider=judge_provider, model=judge_model,
            hunk_id=hunk_id, line=info["line"],
            proposals_count=len(parts), proposals_passes=proposals_passes,
            merged_count=1, latency_ms=latency_ms, usage=usage,
        )
    return merged


def generate_review_comments(request: ReviewRequest) -> List[Dict[str, Any]]:
    """End-to-end review across diff: four passes per hunk, aggregate comments.

    Fetches upstream, builds unified diff, runs defect, refactor, compiler,
    and style passes per hunk, applies confidence/alignment checks, and returns
    GitHub comments.
    
    All tracing is scoped to a PR-specific LangSmith project.
    """
    import os
    
    logger.info(f"Start review PR=#{request.pull_request_number} {request.base_sha}..{request.head_sha}")
    
    pr_project_name = f"escargot-review-bot/PR-{request.pull_request_number}"
    
    # 환경변수를 동적으로 변경하여 LangChain 자동 트레이싱도 PR별 프로젝트로 보냄
    old_project = os.environ.get("LANGCHAIN_PROJECT")
    os.environ["LANGCHAIN_PROJECT"] = pr_project_name
    
    try:
        with ls.tracing_context(project_name=pr_project_name):
            return _execute_review(request)
    finally:
        # 원래 값 복원
        if old_project is not None:
            os.environ["LANGCHAIN_PROJECT"] = old_project
        else:
            os.environ.pop("LANGCHAIN_PROJECT", None)


def _execute_review(request: ReviewRequest) -> List[Dict[str, Any]]:
    """Internal implementation of the review logic."""
    # Fetch upstream refs and ensure base/head SHAs are available locally
    logger.info("Fetching latest data from upstream...")
    fetch_upstream_with_fallback(request.pull_request_number, request.base_sha, request.head_sha)
    logger.info("Fetch complete.")

    # Build unified diff between base..head with configured context lines
    diff_text = run_git_command([
        "diff", "--no-color", "--no-ext-diff", "--text",
        f"-U{DIFF_CONTEXT}", request.base_sha, request.head_sha
    ])
    diff_text = diff_text.replace("\r\n", "\n")
    if not diff_text.endswith("\n"):
        diff_text += "\n"

    try:
        # Parse diff into PatchSet
        patch_set = PatchSet.from_string(diff_text)
        try:
            logger.info(f"Diff created. files={len(patch_set)}")
        except Exception:
            logger.info("Diff created. (could not count files)")
    except Exception as e:
        logger.exception(f"Diff parse failed: {e}")
        return []

    head_blob_cache: Dict[str, List[str]] = {}
    for patched_file in patch_set:
        file_path = patched_file.path
        if not any(file_path.startswith(p) for p in REVIEW_INCLUDE_PATHS):
            continue
        key = f"{request.head_sha}:{file_path}"
        if key not in head_blob_cache:
            try:
                blob_text = run_git_command(["show", f"{request.head_sha}:{file_path}"])
                head_blob_cache[key] = blob_text.splitlines()
            except Exception as e:
                logger.debug(f"Could not load blob {key}: {e}")
                head_blob_cache[key] = []

    hunk_items: List[Tuple[str, Hunk, List[LineMappingLite], Dict[int, Any]]] = []
    for patched_file in patch_set:
        file_path = patched_file.path
        if not any(file_path.startswith(p) for p in REVIEW_INCLUDE_PATHS):
            continue
        for hunk in patched_file:
            mappings = create_line_mappings_for_hunk(hunk)
            if not mappings:
                continue
            mapping_dict = {m.target_id: m for m in mappings}
            hunk_items.append((file_path, hunk, mappings, mapping_dict))

    if not hunk_items:
        logger.info("No hunks to review.")
        return []

    workers = max(1, REVIEW_PARALLEL_WORKERS)
    if OLLAMA_KEEP_ALIVE == "0" and workers > 1 and any_pass_uses_provider("ollama"):
        logger.info(
            f"OLLAMA_KEEP_ALIVE=0 with an ollama pass: forcing workers=1 to avoid model unload race (was {workers})."
        )
        workers = 1

    provider_breakdown: Dict[str, str] = {
        p: f"{resolve_pass_provider(p)}:{resolve_pass_model(p)}" for p in PASS_TYPES
    }
    use_parallel_passes = REVIEW_PARALLEL_PASSES
    models_info = ", ".join(f"{p}={provider_breakdown[p]}" for p in PASS_TYPES)
    if use_parallel_passes:
        logger.info(
            f"Parallel passes enabled: [{models_info}], "
            f"{len(hunk_items)} hunks × 4 passes = {4 * len(hunk_items)} tasks, "
            f"max_workers={min(4 * workers, 4 * len(hunk_items))}."
        )
    else:
        logger.info(f"Sequential review: [{models_info}], {len(hunk_items)} hunks, {workers} workers per pass.")

    if EXPERIMENT_LOGGING_ENABLED:
        import os as _os
        provider_summary = "-".join(
            sorted({resolve_pass_provider(p) for p in PASS_TYPES})
        )
        metrics: MetricsLike = MetricsLogger(
            pr_number=request.pull_request_number,
            provider_summary=provider_summary,
            output_dir=EXPERIMENT_LOG_DIR,
            label=_os.getenv("EXPERIMENT_LABEL") or None,
        )
        logger.info(f"Experiment log: {metrics.path}")
    else:
        metrics = NullMetricsLogger()

    def get_hunk_line_range(hunk: Hunk) -> str:
        """Get line range string for hunk (e.g., 'L42-L58')."""
        start = hunk.target_start
        end = hunk.target_start + hunk.target_length - 1
        if start == end:
            return f"L{start}"
        return f"L{start}-L{end}"

    def run_single_hunk(i: int) -> List[Dict[str, Any]]:
        """Run all passes for a single hunk within a unified trace."""
        fp, h, m, md = hunk_items[i]
        file_name = fp.split('/')[-1]
        line_range = get_hunk_line_range(h)
        trace_name = f"{file_name}_{line_range}"
        
        with ls.trace(
            name=trace_name,
            run_type="chain",
            inputs={
                "file_path": fp,
                "hunk_start": h.target_start,
                "hunk_length": h.target_length,
            },
            metadata={
                "file_path": fp,
                "hunk_index": i,
                "target_start": h.target_start,
                "target_length": h.target_length,
            },
        ) as hunk_run:
            defect_comments = _run_review_pass(
                model_type="defect",
                file_path=fp, hunk=h, mappings=m, mapping_dict=md,
                head_sha=request.head_sha, head_blob_cache=head_blob_cache,
                metrics=metrics, hunk_id=trace_name,
            )

            refactor_comments = _run_review_pass(
                model_type="refactor",
                file_path=fp, hunk=h, mappings=m, mapping_dict=md,
                head_sha=request.head_sha, head_blob_cache=head_blob_cache,
                metrics=metrics, hunk_id=trace_name,
            )

            compiler_comments = _run_review_pass(
                model_type="compiler",
                file_path=fp, hunk=h, mappings=m, mapping_dict=md,
                head_sha=request.head_sha, head_blob_cache=head_blob_cache,
                metrics=metrics, hunk_id=trace_name,
            )

            style_comments = _run_review_pass(
                model_type="style",
                file_path=fp, hunk=h, mappings=m, mapping_dict=md,
                head_sha=request.head_sha, head_blob_cache=head_blob_cache,
                metrics=metrics, hunk_id=trace_name,
            )

            merged = _merge_comments_by_line(
                hunk_items[i],
                defect_comments,
                refactor_comments,
                compiler_comments,
                style_comments,
                metrics=metrics,
                hunk_id=trace_name,
            )
            
            hunk_run.end(outputs={
                "comment_count": len(merged),
                "defect_count": len(defect_comments),
                "refactor_count": len(refactor_comments),
                "compiler_count": len(compiler_comments),
                "style_count": len(style_comments),
            })
            
            return merged

    all_github_comments: List[Dict[str, Any]] = []

    try:
        hunk_indices = list(range(len(hunk_items)))
        if use_parallel_passes:
            chunks = [hunk_indices[i:i + workers] for i in range(0, len(hunk_indices), workers)]
            for chunk_idx, chunk in enumerate(chunks):
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    future_to_idx = {executor.submit(run_single_hunk, i): i for i in chunk}
                    for future in as_completed(future_to_idx):
                        hunk_idx = future_to_idx[future]
                        try:
                            all_github_comments.extend(future.result())
                        except Exception as e:
                            logger.exception(f"Hunk {hunk_idx} review failed: {e}")
                if chunk_idx < len(chunks) - 1:
                    logger.info(f"Batch {chunk_idx + 1}/{len(chunks)} done. Sleeping 90s (TPM rate limit)...")
                    time.sleep(90)
        else:
            for i in hunk_indices:
                try:
                    all_github_comments.extend(run_single_hunk(i))
                except Exception as e:
                    logger.exception(f"Hunk {i} review failed: {e}")
    finally:
        try:
            metrics.finalize(
                total_hunks=len(hunk_items),
                total_comments_posted=len(all_github_comments),
                provider_breakdown=provider_breakdown,
            )
        except Exception as e:
            logger.warning(f"Metrics finalize failed: {e}")

    logger.info(f"Generated {len(all_github_comments)} comments (hunks={len(hunk_items)}, parallel={use_parallel_passes}).")
    return all_github_comments

