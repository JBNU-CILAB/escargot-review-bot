"""JSONL experiment logger for paper-grade comparison runs.

A `MetricsLogger` is created per `_execute_review` call and writes one event per
line to `EXPERIMENT_LOG_DIR/PR-{n}-{providers}-{ts}.jsonl`. Writes are
thread-safe so the `ThreadPoolExecutor` in `service.run_single_hunk` can share
a single logger across hunks.

Events:
  - llm_call      one per LLM round-trip (defect/refactor/compiler/style/judge)
  - pass_summary  per (pass, hunk): raw vs accepted + skip reason histogram
  - judge_call    judge round-trip per merged line
  - pr_summary    final totals (tokens, latency, calls)

Token usage is read from `AIMessage.usage_metadata` (normalized across providers
by LangChain). Cost is intentionally not computed here — the paper can apply
current pricing offline from the recorded token counts.
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional


SKIP_REASONS = (
    "skip_ids",
    "duplicate_in_pass",
    "low_confidence",
    "invalid_target_id",
    "align_failed",
)


def new_skip_reason_counter() -> Dict[str, int]:
    return {r: 0 for r in SKIP_REASONS}


def extract_usage(ai_message: Any) -> Dict[str, Optional[int]]:
    """Pull normalized token counts off an AIMessage; tolerates missing fields."""
    usage = getattr(ai_message, "usage_metadata", None) or {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


class NullMetricsLogger:
    """No-op logger used when EXPERIMENT_LOGGING is disabled.

    Implements the same surface so callers don't need conditional branches.
    """

    path: Optional[Path] = None
    experiment_id: str = ""

    def log_llm_call(self, **_: Any) -> None: ...
    def log_pass_summary(self, **_: Any) -> None: ...
    def log_judge_call(self, **_: Any) -> None: ...
    def finalize(self, **_: Any) -> None: ...


class MetricsLogger:
    def __init__(
        self,
        pr_number: int,
        provider_summary: str,
        output_dir: str,
        label: Optional[str] = None,
    ) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.experiment_id = f"{ts}-{uuid.uuid4().hex[:6]}"
        self.pr_number = pr_number
        self.provider_summary = provider_summary
        self.label = label
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        # Filename uses label if set (replay profile name); otherwise provider_summary.
        # Either way the JSONL records both so downstream analysis can group cleanly.
        name_tag = label or provider_summary
        self.path = Path(output_dir) / (
            f"PR-{pr_number}-{name_tag}-{self.experiment_id}.jsonl"
        )
        self._fh = self.path.open("a", encoding="utf-8")
        self._lock = Lock()
        self._start_ts = time.time()
        self._totals: Dict[str, int] = {
            "llm_calls": 0,
            "llm_errors": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }

    def _write(self, event: Dict[str, Any]) -> None:
        event.setdefault("experiment_id", self.experiment_id)
        event.setdefault("pr_number", self.pr_number)
        event.setdefault("ts", datetime.now(timezone.utc).isoformat())
        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def log_llm_call(
        self,
        *,
        pass_type: str,
        provider: str,
        model: str,
        hunk_id: str,
        latency_ms: int,
        usage: Dict[str, Optional[int]],
        raw_comments_count: int,
        error: Optional[str] = None,
    ) -> None:
        self._write({
            "event": "llm_call",
            "pass_type": pass_type,
            "provider": provider,
            "model": model,
            "hunk_id": hunk_id,
            "latency_ms": latency_ms,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "raw_comments_count": raw_comments_count,
            "error": error,
        })
        with self._lock:
            self._totals["llm_calls"] += 1
            if error:
                self._totals["llm_errors"] += 1
            self._totals["input_tokens"] += usage.get("input_tokens") or 0
            self._totals["output_tokens"] += usage.get("output_tokens") or 0

    def log_pass_summary(
        self,
        *,
        pass_type: str,
        hunk_id: str,
        raw_comments: int,
        accepted: int,
        skip_reasons: Dict[str, int],
    ) -> None:
        self._write({
            "event": "pass_summary",
            "pass_type": pass_type,
            "hunk_id": hunk_id,
            "raw_comments": raw_comments,
            "accepted": accepted,
            "skip_reasons": skip_reasons,
        })

    def log_judge_call(
        self,
        *,
        provider: str,
        model: str,
        hunk_id: str,
        line: int,
        proposals_count: int,
        proposals_passes: List[str],
        merged_count: int,
        latency_ms: int,
        usage: Dict[str, Optional[int]],
        error: Optional[str] = None,
    ) -> None:
        self._write({
            "event": "judge_call",
            "provider": provider,
            "model": model,
            "hunk_id": hunk_id,
            "line": line,
            "proposals_count": proposals_count,
            "proposals_passes": proposals_passes,
            "merged_count": merged_count,
            "latency_ms": latency_ms,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "error": error,
        })
        with self._lock:
            self._totals["llm_calls"] += 1
            if error:
                self._totals["llm_errors"] += 1
            self._totals["input_tokens"] += usage.get("input_tokens") or 0
            self._totals["output_tokens"] += usage.get("output_tokens") or 0

    def finalize(
        self,
        *,
        total_hunks: int,
        total_comments_posted: int,
        provider_breakdown: Dict[str, str],
    ) -> None:
        self._write({
            "event": "pr_summary",
            "label": self.label,
            "provider_summary": self.provider_summary,
            "provider_breakdown": provider_breakdown,
            "total_hunks": total_hunks,
            "total_comments_posted": total_comments_posted,
            "total_llm_calls": self._totals["llm_calls"],
            "total_llm_errors": self._totals["llm_errors"],
            "total_input_tokens": self._totals["input_tokens"],
            "total_output_tokens": self._totals["output_tokens"],
            "total_duration_sec": round(time.time() - self._start_ts, 3),
        })
        with self._lock:
            self._fh.close()
