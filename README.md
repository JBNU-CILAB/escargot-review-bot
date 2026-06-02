# Escargot Review Bot
A self‑hosted review server with GitHub Actions integration that generates automated code‑review comments for pull requests in the Escargot (lightweight ECMAScript engine) repository. It provides a Four-Pass LLM Review (Defect, Refactor, Compiler, Style — sequential within each hunk, parallel across hunks), intelligent Judge Aggregation, robust line anchoring, and incremental review‑on‑push behavior, all powered by the LangChain framework.

## Purpose
Built to shorten review time and reduce feedback latency so contributors can submit PRs confidently and receive an early quality signal (typically within minutes). When a PR is created or updated, only the changed lines are analyzed to propose concrete defect and refactoring candidates, with comments precisely anchored to newly added lines as evidence.

For each hunk, the pipeline runs 4 specialized passes **sequentially** (Defect → Refactor → Compiler → Style) so that lower-priority passes can skip lines already flagged by higher-priority passes. Hunks themselves are processed in parallel (`REVIEW_PARALLEL_WORKERS`). Any residual overlap on the same `(path, line)` is then merged by a Judge pass to prevent self-confirmation bias. Runs on either a local LLM (Ollama) or OpenAI, integrates cleanly with GitHub Actions, and is suited for self-hosted runners.

- Generates reviews tailored specifically for the lightweight JavaScript engine Escargot.
 - Analyze PR diffs to automatically propose defect and improvement candidates across 4 diverse perspectives.
- Generate safe, well-grounded comments anchored to newly added lines.
- **Judge Aggregation:** Merges multiple comments targeting the same line based on priority (Defect ≥ Refactor ≥ {Compiler, Style}).
- Leverage a local LLM (Ollama) with LangChain for stable, hunk-parallel executions and cost-efficient reviews.
- **LangSmith Tracing:** Systematically tracks PR review data to continuously analyze vulnerabilities and improve bot accuracy.
- Integrate seamlessly with GitHub Actions to post comments.

## Key features
- Sequential four-pass review per hunk (parallel across hunks)
  - Each hunk runs Defect → Refactor → Compiler → Style **sequentially** (the order also conveys priority to the Judge). Hunks themselves are processed in parallel via `REVIEW_PARALLEL_WORKERS`.
- Judge-merge redundancy suppression
  - The Judge pass merges any `(path, line)` overlap between passes into a single comment via an independent evaluation, mitigating self-confirmation bias. Priority `Defect ≥ Refactor ≥ {Compiler, Style}` is communicated to the Judge by sorting proposals in pass order.
- Robust line anchoring (against HEAD)
  - Exact match → windowed nearby search (±`ALIGN_SEARCH_WINDOW`) → ±2‑line context tiebreaker; HEAD blobs cached per `{sha}:{path}`
- Incremental review (workflow integration)
  - On `synchronize`, analyze only `before..head`; per‑PR `concurrency` with `cancel-in-progress`; `DIFF_CONTEXT` applied to diffs
- Path scoping
  - Restrict review to prefixes in `REVIEW_INCLUDE_PATHS` (focus on engine‑critical directories)
- LangChain Integration & Tracing
  - LLM orchestration migrated to standard LangChain, enabling detailed LangSmith tracing for vulnerability analysis and easy integration of new LLM providers.
- Git and error handling
  - Map git subprocess failures to HTTP 500; rich debug logs for traceability
- Performance
  - Tunable hunk-parallel workers (`REVIEW_PARALLEL_WORKERS`). Note: `OLLAMA_TIMEOUT_SECONDS`, `OLLAMA_MAX_RETRIES`, and `INTER_REQUEST_DELAY_SECONDS` apply only to the legacy `chat_and_parse` path and are **not** wired into the current `ChatOllama` / `ChatOpenAI` pipeline.

## Architecture overview
```text
[GitHub Actions (pull_request_target)]
   ├─ Compute diff range
   │    • opened/reopened  → base = PR base SHA, head = PR head SHA (full review)
   │    • synchronize      → base = before,      head = PR head SHA (incremental)
   ├─ POST /review {base, head, pr}
   ├─ Receive review.json {comments: [...]}
   └─ POST /pulls/{pr}/comments (loop, 200ms interval)

[Review Server (FastAPI + LangChain)]
   ├─ fetch_upstream_with_fallback(upstream, PR ref, SHAs)
   ├─ git diff -U{DIFF_CONTEXT} base head
   ├─ Parse PatchSet (unidiff)
   ├─ For each file filtered by REVIEW_INCLUDE_PATHS
   │    └─ For each hunk (hunks parallelized via REVIEW_PARALLEL_WORKERS)
   │         ├─ create_line_mappings_for_hunk → target_id indexing
   │         ├─ build_hunk_based_prompt (added lines only)
   │         ├─ Sequential LLM Passes (LangChain), independent per pass:
   │         │    ├─ Defect Pass
   │         │    ├─ Refactor Pass
   │         │    ├─ Compiler Pass
   │         │    └─ Style Pass
   │         ├─ Judge Pass: merge overlapping (path, line) comments
   │         │                 Priority: Defect ≥ Refactor ≥ {Compiler, Style}
   │         └─ HEAD alignment → nearby search(±ALIGN_SEARCH_WINDOW)
   │              → ±2-line context tiebreaker → GitHubComment(line/side)
   └─ Return {comments: [...]}

[Adapters & Observability]
   • Git: run_git_command (diff/show/fetch), HEAD blob cache
   • LLM Orchestration: LangChain chat models (ChatOllama / ChatOpenAI), JSON parsing
   • Tracing: LangSmith (Performance & Vulnerability Analysis tracking)
```

### Request → response pipeline (summary)
1) Actions computes `base/head/pr` and calls the server (`synchronize` uses `before..head`).
2) The server syncs upstream and runs `git diff -U{DIFF_CONTEXT}` to produce a unified diff.
3) Parse the PatchSet with `unidiff`, then iterate per file/hunk (paths limited by `REVIEW_INCLUDE_PATHS`).
4) For each hunk, collect only `added` lines into a catalog (unique `target_id`) and build the prompt.
5) Call the LLM for 4 sequential passes within the hunk (Defect → Refactor → Compiler → Style) using LangChain. The passes run independently; redundancy on the same `(path, line)` is collapsed downstream by the Judge. Different hunks run in parallel.
6) Valid JSON arrays are extracted, parsed, and validated via Pydantic schema models.
7) Judge pass: Comments from different passes that resolve to the same `(path, line)` are grouped and passed to the Judge model. The Judge evaluates and merges them into a single readable comment based on predefined priorities (Defect ≥ Refactor ≥ {Compiler, Style}), effectively preventing self-confirmation bias.
8) Line anchoring: if exact HEAD alignment fails, search nearby (±`ALIGN_SEARCH_WINDOW`), then use ±2-line context to pick a unique candidate.
9) Convert valid items to `GitHubComment` with `commit_id/path/line/side` and accumulate.
10) Return `{comments:[...]}` to Actions, which posts PR review comments.

### Components
- API: `escargot_review_bot/api.py` (FastAPI, `POST /review`)
- Service: `escargot_review_bot/service.py` (diff/parsing/prompting/Judge aggregation)
- Adapters: `adapters/git.py` (git subprocess), `adapters/llm.py` (LangChain integration)
- Config/Logging: `config/config.py` (env vars), `config/logging.py` (stdout-only logger)
- Schemas/Prompts: `domain/schemas.py` (Pydantic), `prompts/*` (Defect/Compiler/Refactor/Style/Judge prompts)

## Guards and safety checks
- JSON-only output enforcement: LLM responses must be valid JSON arrays; schema-validated via Pydantic models.
- Confidence threshold: suggestions below `CONFIDENCE_THRESHOLD` are dropped.
- Path scoping: only files under `REVIEW_INCLUDE_PATHS` are considered.
- HEAD anchoring: exact match first, then nearby search (±`ALIGN_SEARCH_WINDOW`) with ±2-line context tiebreaker; ambiguous matches are skipped.
- Judge-merge redundancy suppression: the Judge pass merges any `(path, line)` overlap between passes using an independent evaluation, mitigating self-confirmation bias. Pass priority `Defect ≥ Refactor ≥ {Compiler, Style}` is conveyed by the order proposals are presented to the Judge.
- Git safety: subprocess failures mapped to HTTP 500; upstream fetch with fallbacks; HEAD blobs cached per `{sha}:{path}`.
- Concurrency guard (workflow): per-PR group with `cancel-in-progress: true` cancels older runs on new pushes.
- LLM robustness: pass-level failures (LLM invoke errors, JSON parse failures, Pydantic validation errors) are logged and skipped — one bad hunk returns an empty comment list rather than aborting the whole review. The chat models built by `_build_chat_model` do **not** set request timeouts; `OLLAMA_TIMEOUT_SECONDS` / `OLLAMA_MAX_RETRIES` apply only to the legacy `chat_and_parse` path, which the current pipeline does not invoke.

## Requirements
- Python 3.11+ (tested on 3.12)
- Git installed, with network access to fetch from the `upstream` remote
  - `REPO_PATH` must be a valid local clone and have an `upstream` remote configured
- Ollama installed and the target models pulled (e.g., `qwen3-coder:30b`)
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

## Operations (systemd + journald)

In production, the service is managed by systemd and logs are viewed via journald. Detailed configuration (service user/group, paths, EnvironmentFile, ports) and any security‑sensitive values are maintained in external operations documentation and are not tracked in this repository.

1) Unit file location
- The unit is assumed to be provisioned at `/etc/systemd/system/escargot-review-bot.service`. Its contents are intentionally not documented here.

2) Enable and start
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now escargot-review-bot
```

3) Restart / stop / status
```bash
sudo systemctl restart escargot-review-bot
sudo systemctl stop escargot-review-bot
sudo systemctl status escargot-review-bot --no-pager
```

4) Logs (journald)
```bash
# Jump to the end
journalctl -u escargot-review-bot -e

# Follow live, starting with the last 200 lines
journalctl -u escargot-review-bot -f -n 200

# Current boot only
journalctl -u escargot-review-bot -b
```

5) Applying updates (example)
```bash
cd /ABS/PATH/TO/escargot-review-bot
source /ABS/PATH/TO/venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart escargot-review-bot
```

### Environment variables (.env)
| Key | Default | Description |
|---|---:|---|
| `REPO_PATH` | (required) | Absolute path to the local Escargot clone used as the git working directory. Must exist and contain an `upstream` remote. |
| `LANGCHAIN_TRACING_V2` | `true` | Enables LangSmith tracing to collect and analyze PR review data. |
| `LANGCHAIN_API_KEY` | (required) | LangSmith API Key. |
| `LANGCHAIN_PROJECT` | `escargot-review-bot`| LangSmith project name for traces. |
| `REVIEW_PARALLEL_WORKERS`| `3` | Number of parallel workers for hunk concurrency. |
| `REVIEW_PARALLEL_PASSES` | `true` | Enables parallel execution for Defect, Compiler, Refactor, and Style passes. |
| `OLLAMA_KEEP_ALIVE` | `30m` | Keeps the model loaded in memory for the specified duration. |
| `MODEL_DEFECT` | `model-name` | Model used for the Defect pass (provider-agnostic). |
| `MODEL_REFACTOR` | `model-name` | Model used for the Refactor pass (provider-agnostic). |
| `MODEL_COMPILER` | `model-name` | Model used for the Compiler pass (provider-agnostic). |
| `MODEL_STYLE` | `model-name` | Model used for the Style pass (provider-agnostic). |
| `MODEL_JUDGE` | `model-name` | Model used for the Judge merge pass (provider-agnostic). |
| `OLLAMA_TEMPERATURE` | `0.1` | Sampling temperature for the LLM. |
| `OLLAMA_NUM_CTX` | `8192` | Context window size passed to Ollama. |
| `OLLAMA_NUM_BATCH` | `256` | Batch size for Ollama generation. |
| `OLLAMA_REPEAT_PENALTY` | `1.1` | Repeat penalty for Ollama generation. |
| `CONFIDENCE_THRESHOLD` | `0.8` | Minimum confidence required for an LLM suggestion to be kept (0.0–1.0). |
| `OLLAMA_TIMEOUT_SECONDS` | `1800` | Per‑request timeout (seconds) for LLM API calls. |
| `OLLAMA_MAX_RETRIES` | `2` | Retry attempts on timeouts/unexpected parsing errors. |
| `INTER_REQUEST_DELAY_SECONDS`| `0` | Delay (seconds) to mitigate rate limits during parallel requests. |


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
