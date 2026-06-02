import os
from typing import List

from dotenv import load_dotenv


# Load environment variables from the project root .env
load_dotenv()


# Core repository settings
REPO_PATH = os.getenv("REPO_PATH")
if not REPO_PATH or not os.path.isdir(REPO_PATH):
    raise ValueError(f"REPO_PATH '{REPO_PATH}' is not a valid directory")


# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG").upper()


# Concurrency for /review endpoint
REVIEW_MAX_CONCURRENCY = int(os.getenv("REVIEW_MAX_CONCURRENCY", "1"))


# Review bot settings
DIFF_CONTEXT = int(os.getenv("DIFF_CONTEXT", "10"))
REVIEW_PARALLEL_WORKERS = int(os.getenv("REVIEW_PARALLEL_WORKERS", "4"))
REVIEW_PARALLEL_PASSES = os.getenv("REVIEW_PARALLEL_PASSES", "false").lower() in ("1", "true", "yes")
REVIEW_INCLUDE_PATHS: List[str] = [
    p.strip() for p in os.getenv("REVIEW_INCLUDE_PATHS", "src/").split(",") if p.strip()
]


# LLM provider selection
# Re-read per call so changes between requests (e.g. ablation scripts) take effect
# without restarting the server.
PASS_TYPES = ("defect", "refactor", "compiler", "style", "judge")


def resolve_pass_provider(pass_type: str) -> str:
    """Per-pass provider override, falling back to LLM_PROVIDER, then 'ollama'."""
    return (
        os.getenv(f"PROVIDER_{pass_type.upper()}")
        or os.getenv("LLM_PROVIDER")
        or "ollama"
    ).lower()


def resolve_pass_model(pass_type: str) -> str:
    """Resolve model for a pass from MODEL_{PASS} env."""
    return os.getenv(f"MODEL_{pass_type.upper()}") or ""


def any_pass_uses_provider(provider: str) -> bool:
    """True if any of the 5 passes is configured to use the given provider."""
    return any(resolve_pass_provider(p) == provider for p in PASS_TYPES)


# Ollama configuration
MODEL_NAME = os.getenv("OLLAMA_MODEL", "qwen3-coder:30b")
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "0")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "")  # e.g. http://127.0.0.1:7777; empty → ChatOllama default (11434)

OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.1"))
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
OLLAMA_NUM_BATCH = int(os.getenv("OLLAMA_NUM_BATCH", "256"))
OLLAMA_REPEAT_PENALTY = float(os.getenv("OLLAMA_REPEAT_PENALTY", "1.1"))
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.8"))
ALIGN_SEARCH_WINDOW = int(os.getenv("ALIGN_SEARCH_WINDOW", "25"))
OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "10800"))  # 3-hour window: 10800s per request
OLLAMA_MAX_RETRIES = int(os.getenv("OLLAMA_MAX_RETRIES", "2"))
INTER_REQUEST_DELAY_SECONDS = float(os.getenv("INTER_REQUEST_DELAY_SECONDS", "5"))


# OpenAI configuration (used when LLM_PROVIDER=openai or PROVIDER_*=openai)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "4096"))


# Experiment / measurement
EXPERIMENT_LOG_DIR = os.getenv("EXPERIMENT_LOG_DIR", "experiments")
EXPERIMENT_LOGGING_ENABLED = os.getenv("EXPERIMENT_LOGGING", "true").lower() in ("1", "true", "yes")
