import os
import sys


_MODE_CHOICES = ("sequential", "hunk", "pass")
_MODE_LABELS = {
    "sequential": "sequential  — per-file phase batching, fully serial (workers ignored)",
    "hunk":       "hunk        — ThreadPool(workers) across hunks, 4 passes sequential per hunk  (default)",
    "pass":       "pass        — ThreadPool(workers) across hunks, 4 passes concurrent per hunk",
}


def _maybe_prompt_parallelism() -> None:
    """Interactive parallelism prompt for `python main.py` startup.

    Skipped when:
      - stdin is not a TTY (systemd, CI, piped input), or
      - REVIEW_PARALLELISM is already set in the environment.

    Selections are written to os.environ so config.py picks them up on import.
    Falls back silently to defaults if questionary is not installed.
    """
    if not sys.stdin.isatty():
        return
    if os.environ.get("REVIEW_PARALLELISM"):
        return

    try:
        import questionary
    except ImportError:
        print("[startup] questionary not installed; using REVIEW_PARALLELISM default (hunk).", flush=True)
        return

    # Two-stage color cue:
    #   navigating  → cyan pointer + cyan highlight on the hovered row
    #   confirmed   → green bold on the chosen value (replaces questionary's
    #                 default orange so the "locked in" moment reads visually)
    style = questionary.Style([
        ("qmark",       "fg:ansicyan bold"),
        ("question",    "bold"),
        ("pointer",     "fg:ansicyan bold"),
        ("highlighted", "fg:ansicyan bold"),
        ("answer",      "fg:ansigreen bold"),
        ("instruction", "fg:ansibrightblack"),
    ])

    try:
        mode = questionary.select(
            "Parallelism mode:",
            choices=[questionary.Choice(_MODE_LABELS[m], value=m) for m in ("hunk", "sequential", "pass")],
            default="hunk",
            style=style,
        ).ask()
    except KeyboardInterrupt:
        print("\n[startup] aborted.", flush=True)
        sys.exit(130)

    if mode is None:
        sys.exit(130)

    os.environ["REVIEW_PARALLELISM"] = mode

    if mode in ("hunk", "pass"):
        default_workers = os.environ.get("REVIEW_PARALLEL_WORKERS", "4")
        def _validate(v: str) -> "bool | str":
            if v.isdigit() and 1 <= int(v) <= 16:
                return True
            return "Enter an integer 1-16"
        try:
            workers = questionary.text(
                "Workers (1-16):",
                default=default_workers,
                validate=_validate,
                style=style,
            ).ask()
        except KeyboardInterrupt:
            print("\n[startup] aborted.", flush=True)
            sys.exit(130)
        if workers:
            os.environ["REVIEW_PARALLEL_WORKERS"] = workers

    print(
        f"[startup] parallelism={os.environ['REVIEW_PARALLELISM']}, "
        f"workers={os.environ.get('REVIEW_PARALLEL_WORKERS', '-')}.",
        flush=True,
    )


if __name__ == "__main__":
    _maybe_prompt_parallelism()

    try:
        import uvicorn
    except Exception:
        print("uvicorn is required to run the server. Install dependencies first.")
        sys.exit(1)

    # Defer api import until env vars from the TUI are visible to config.py
    from escargot_review_bot.api import app
    uvicorn.run(app, host="0.0.0.0", port=8000)
