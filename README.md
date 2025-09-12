# Escargot Review Bot
A self‑hosted review server with GitHub Actions integration that generates automated code‑review comments for pull requests in the Escargot (lightweight ECMAScript engine) repository. It provides a two‑pass LLM review (defects then refactors), robust line anchoring, and incremental review‑on‑push behavior.


## Purpose
Built to shorten review time and reduce feedback latency so contributors can submit PRs confidently and receive an early quality signal (typically within ~60 minutes). When a PR is created or updated, only the changed lines are analyzed to propose concrete defect and refactoring candidates, with comments precisely anchored to newly added lines as evidence. This automates initial triage and routine feedback, enabling authors to self-correct and refactor before requesting a human review. Everything runs on a local LLM and a self-hosted runner for speed and cost efficiency, and integrates cleanly with GitHub Actions without altering existing workflows.

 - Generates reviews tailored specifically for the lightweight JavaScript engine Escargot.
 - Analyze PR diffs to automatically propose defect and improvement candidates.
 - Generate safe, well-grounded comments anchored to newly added lines.
 - Leverage a local LLM (Ollama) for fast and cost-efficient reviews.
 - Integrate with GitHub Actions to post comments


## Key features
- Two‑pass LLM review
  - Hunk‑scoped prompts, JSON‑only output with schema validation, confidence threshold, and cross‑pass de‑duplication
- Streaming parsing with safe fallbacks
  - Early detection of a complete JSON array while streaming; conservative sanitize fallback on failure
- Robust line anchoring (against HEAD)
  - Exact match → windowed nearby search (±`ALIGN_SEARCH_WINDOW`) → ±2‑line context tiebreaker; HEAD blobs cached per `{sha}:{path}`
- Incremental review (workflow integration)
  - On `synchronize`, analyze only `before..head`; per‑PR `concurrency` with `cancel-in-progress`; `DIFF_CONTEXT` applied to diffs
- Path scoping
  - Restrict review to prefixes in `REVIEW_INCLUDE_PATHS` (focus on engine‑critical directories)
- Git and error handling
  - Map git subprocess failures to HTTP 500; rich debug logs for traceability
- Performance and timeouts
  - Tunable `OLLAMA_TIMEOUT_SECONDS` (600 by default), `OLLAMA_MAX_RETRIES`, `INTER_REQUEST_DELAY_SECONDS`
- Operational simplicity
  - Single‑review concurrency by default (`REVIEW_MAX_CONCURRENCY=1`), easy `.env` tuning, designed for self‑hosted runners


## Architecture overview
```
[GitHub Actions (pull_request_target)]
   ├─ Compute diff range
   │    • opened/reopened  → base = PR base SHA, head = PR head SHA (full review)
   │    • synchronize      → base = before,      head = PR head SHA (incremental)
   ├─ POST /review {base, head, pr}
   ├─ Receive review.json {comments: [...]}
   └─ POST /pulls/{pr}/comments (loop, 200ms interval)

[Review Server (FastAPI)]
   ├─ fetch_upstream_with_fallback(upstream, PR ref, SHAs)
   ├─ git diff -U{DIFF_CONTEXT} base head
   ├─ Parse PatchSet (unidiff)
   ├─ For each file filtered by REVIEW_INCLUDE_PATHS
   │    └─ For each hunk
   │         ├─ create_line_mappings_for_hunk → target_id indexing
   │         ├─ build_hunk_based_prompt (Commentable Catalog: added lines only)
   │         ├─ Pass 1: defect → chat_and_parse (Ollama stream) → filter pipeline
   │         ├─ Pass 2: refactor (skip target_ids accepted in defect)
   │         └─ HEAD alignment → nearby search(±ALIGN_SEARCH_WINDOW)
   │              → ±2-line context tiebreaker → GitHubComment(line/side)
   └─ Return {comments: [...]}

[Adapters]
   • Git: run_git_command (diff/show/fetch), HEAD blob cache
   • LLM: Ollama (chat stream, timeout/retry), JSON sanitize fallback
```

### Request → response pipeline (summary)
1) Actions computes `base/head/pr` and calls the server (`synchronize` uses `before..head`).
2) The server syncs upstream and runs `git diff -U{DIFF_CONTEXT}` to produce a unified diff.
3) Parse the PatchSet with `unidiff`, then iterate per file/hunk (paths limited by `REVIEW_INCLUDE_PATHS`).
4) For each hunk, collect only `added` lines into a catalog (unique `target_id`) and build the prompt.
5) Call the LLM in two passes: Defect → Refactor.
6) While streaming, detect a complete JSON array early; otherwise, sanitize to recover safely.
7) Apply the filter pipeline: schema validation → cross-pass de-dup → `CONFIDENCE_THRESHOLD` → `target_id` validity.
8) Line anchoring: if exact HEAD alignment fails, search nearby (±`ALIGN_SEARCH_WINDOW`), then use ±2-line context to pick a unique candidate.
9) Convert valid items to `GitHubComment` with `commit_id/path/line/side` and accumulate.
10) Return `{comments:[...]}` to Actions, which posts PR review comments.

### Components
- API: `escargot_review_bot/api.py` (FastAPI, `POST /review`, concurrency gate)
- Service: `escargot_review_bot/service.py` (diff/parsing/prompting/anchoring/filtering)
- Adapters: `adapters/git.py` (git subprocess), `adapters/llm.py` (Ollama stream, timeout/retry)
- Config/Logging: `config/config.py` (env vars), `config/logging.py` (stdout-only logger)
- Schemas/Prompts: `domain/schemas.py` (Pydantic), `prompts/*` (Defect/Refactor system prompts)


## Guards and safety checks
- JSON-only output enforcement: LLM responses must be valid JSON arrays; schema-validated via Pydantic models.
- Confidence threshold: suggestions below `CONFIDENCE_THRESHOLD` are dropped.
- Path scoping: only files under `REVIEW_INCLUDE_PATHS` are considered.
- Diff context normalization: `-U{DIFF_CONTEXT}` and line normalization reduce whitespace noise.
- HEAD anchoring: exact match first, then nearby search (±`ALIGN_SEARCH_WINDOW`) with ±2-line context tiebreaker; ambiguous matches are skipped.
- Cross-pass de-duplication: identical `target_id`s from refactor are skipped if already accepted by defect.
- Git safety: subprocess failures mapped to HTTP 500; upstream fetch with fallbacks; HEAD blobs cached per `{sha}:{path}`.
- Concurrency guard (server): global semaphore `REVIEW_MAX_CONCURRENCY` (default 1) serializes `/review` requests.
- Concurrency guard (workflow): per-PR group with `cancel-in-progress: true` cancels older runs on new pushes.
- LLM robustness: per-request timeout (`OLLAMA_TIMEOUT_SECONDS`), retries (`OLLAMA_MAX_RETRIES`), early-stop on complete arrays, sanitize fallback on malformed output, optional pacing (`INTER_REQUEST_DELAY_SECONDS`).


## Requirements
- Python 3.11+ (tested on 3.12)
- Git installed, with network access to fetch from the `upstream` remote
  - `REPO_PATH` must be a valid local clone and have an `upstream` remote configured
- Ollama installed and the target model pulled (default: `gpt-oss:20b`)
- Self-hosted GitHub Runner that can reach the review server (localhost or network)
- OS: Linux recommended


## Installation
```bash
# 1) Create and activate a virtual environment
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2) Configure environment variables (.env)
cp .env.example .env && vi .env

# 3) Run the server (development)
python -m escargot_review_bot.main
# or
uvicorn escargot_review_bot.api:app --host 0.0.0.0 --port 8000
```

### Environment variables (.env)
| Key | Default | Description |
|---|---:|---|
| `REPO_PATH` | (required) | Absolute path to the local Escargot clone used as the git working directory (e.g., `/home/runner/work/escargot/escargot`). Must exist and contain an `upstream` remote accessible by the server. |
| `LOG_LEVEL` | `DEBUG` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `REVIEW_MAX_CONCURRENCY` | `1` | Max concurrent `/review` requests. Keep `1` for self‑hosted stability (requests are effectively queued). |
| `DIFF_CONTEXT` | `10` | Lines of context for `git diff -U{n}`. Larger values can help anchoring by providing more surrounding lines. |
| `REVIEW_INCLUDE_PATHS` | `src/` | Comma‑separated path prefixes to include in review (e.g., `src/,runtime/,interpreter/`). Only files starting with these prefixes are analyzed. |
| `OLLAMA_MODEL` | `gpt-oss:20b` | Ollama model name. |
| `OLLAMA_TEMPERATURE` | `0.1` | Sampling temperature for the LLM. |
| `OLLAMA_NUM_CTX` | `8192` | Context window size passed to Ollama. |
| `OLLAMA_NUM_BATCH` | `256` | Batch size for Ollama generation. |
| `OLLAMA_REPEAT_PENALTY` | `1.1` | Repeat penalty for Ollama generation. |
| `CONFIDENCE_THRESHOLD` | `0.8` | Minimum confidence required for an LLM suggestion to be kept (0.0–1.0). |
| `ALIGN_SEARCH_WINDOW` | `25` | Window size (in lines) for nearby search when aligning comments to HEAD. |
| `OLLAMA_TIMEOUT_SECONDS` | `600` | Per‑request timeout (seconds) for LLM streaming calls. |
| `OLLAMA_MAX_RETRIES` | `2` | Retry attempts on timeouts/unexpected errors. |
| `INTER_REQUEST_DELAY_SECONDS` | `5` | Delay (seconds) between LLM requests to avoid resource saturation. |


## GitHub Actions integration (incremental review)
Workflow file: `.github/workflows/code-review.yml` in `Samsung/escargot`.

- Triggers: `pull_request_target` on `opened`, `synchronize`, `reopened`.
- Diff range selection:
  - `opened`/`reopened`: `base = pr.base.sha`, `head = pr.head.sha` (full review)
  - `synchronize`: `base = before`, `head = pr.head.sha` (only the latest push)
- Steps:
  1) Compute range (github-script)
  2) POST to review server: `POST $REVIEW_SERVER/review` with `{base_sha, head_sha, pull_request_number}`
  3) Read `review.json` and post PR comments (200 ms spacing)
- Within the same PR: when a new push arrives, any in‑progress older run is canceled (`cancel-in-progress: true`), so only the latest push gets reviewed. Runs for other PRs may still execute in parallel.


## API
- Endpoint: `POST /review`
- Request (JSON):
```json
{
  "base_sha": "<40-hex>",
  "head_sha": "<40-hex>",
  "pull_request_number": 123
}
```
- Response (JSON):
```json
{
  "comments": [
    {
      "path": "src/file.cpp",
      "body": "comment body",
      "commit_id": "<head sha>",
      "line": 42,
      "side": "RIGHT"
    }
  ]
}
```