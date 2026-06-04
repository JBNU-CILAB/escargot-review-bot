"""Ollama 실험 배치 실행 스크립트 (SSH 연결 종료 후에도 지속 실행)

run_openai.py 의 Ollama 버전이다. 차이는 provider 를 ollama 로 강제하고
MODEL_* / EXPERIMENT_LABEL 을 ollama 모델명에 맞추는 것뿐이며, diff/매핑/필터/
judge 병합 등 파이프라인 코드 경로는 완전히 동일하다.

모드별 worker 수 (이 스크립트의 핵심)
------------------------------------
    sequential → worker 1 (애초에 worker 무시)
    hunk       → worker N (기본 3, --workers 로 변경)
    pass       → worker N (기본 3, --workers 로 변경)

왜 모드마다 별도 프로세스인가
-----------------------------
`REVIEW_PARALLELISM`/`REVIEW_PARALLEL_WORKERS`/`OLLAMA_KEEP_ALIVE` 는
`config.config` 가 처음 import 될 때 모듈 상수로 **고정**된다. 따라서 한 프로세스
안에서는 모드/worker 를 바꿀 수 없다. 여러 모드를 요청하면 이 스크립트는 모드마다
자기 자신을 단일 모드(`--modes <한개>`)로 서브프로세스 재실행하여, 각 자식이 새
config import 로 올바른 모드/worker 를 고정하도록 한다.

KEEP_ALIVE 가드 주의
--------------------
`OLLAMA_KEEP_ALIVE=0` 이면 service.py 가드가 hunk→workers 1, pass→sequential 로
강제 클램프한다(모델 언로드 레이스 방지). 이 스크립트는 모든 패스를 **같은 모델 하나**
로 돌리므로 swap thrash 가 없어, 기본값을 `--keep-alive 30m` 으로 두어 가드를 끄고
hunk/pass 의 worker 동시성을 그대로 살린다. `--keep-alive 0` 으로 주면 가드가 다시
켜져 worker 가 1 로 클램프되니 주의.
(실제 동시 실행 수는 Ollama 서버의 OLLAMA_NUM_PARALLEL 에도 좌우됨 — 서버 설정.)

실행 방법
---------
    # 포그라운드 (터미널에서 직접) — 기본: 3모드 모두, hunk/pass=3, sequential=1
    python scripts/run_ollama.py

    # 백그라운드 (SSH 끊겨도 계속 실행)
    nohup python scripts/run_ollama.py > experiments/run.log 2>&1 &
    tail -f experiments/run.log   # 로그 확인

    # 모델 변경 (로컬에 pull 돼 있어야 함: `ollama list` 로 확인)
    python scripts/run_ollama.py --model deepseek-coder-v2:lite

    # 모드 변경
    python scripts/run_ollama.py --modes sequential    # 특정 모드만
    python scripts/run_ollama.py --modes hunk pass     # 일부 모드만

    # hunk/pass worker 수 변경 (sequential 은 항상 1)
    python scripts/run_ollama.py --workers 4

    # GPU 샘플링 끄기 (nvidia-smi 없는 환경, 또는 오버헤드 제거 시)
    python scripts/run_ollama.py --gpu-interval 0

    # LangSmith 프로젝트명에 회차 번호 붙이기 (예: escargot-review-bot/01-PR-4-...)
    python scripts/run_ollama.py --run-number 01

결과 파일 (experiments/ 디렉토리)
---------------------------------
    PR-{n}-{model}-{ts}-comments.json  : GitHub에 올라갈 코멘트 원본 (모델명의 ':' 은 '-' 로 치환)
    PR-{n}-ollama-{model}-{ts}.jsonl   : metrics (토큰수, latency 등 — service.py MetricsLogger)
    PR-{n}-{model}-{ts}-gpu.json       : 리뷰 구간 GPU 사용률 요약(mean/max/p95) — nvidia-smi 샘플링
                                         (--gpu-interval 0 또는 nvidia-smi 부재 시 생성 안 함)

GPU 지표 주의 (vGPU)
--------------------
이 머신은 vGPU 프로파일(RTXA6000-48C)이라 nvidia-smi 가 power.draw/temperature 를
[N/A] 로 막는다 → 전력·에너지·온도는 수집 불가. 수집되는 것: SM 사용률(%),
메모리 대역폭 사용률(%), VRAM 사용량(MiB), SM 클럭(MHz).

전제 조건 / 주의
----------------
- Ollama 데몬이 떠 있고 해당 모델이 pull 돼 있어야 한다(`ollama list`). 데몬 주소는
  .env 의 OLLAMA_BASE_URL 로 설정한다(미설정 시 ChatOllama 기본 :11434).
- service.py 의 90초 배치 sleep 은 OpenAI TPM 대비책이며, ollama 패스가 하나라도
  있으면 자동으로 건너뛴다(any_pass_uses_provider("ollama") 게이트).
"""

# ============================================================
# 실험 대상 PR 목록 — 여기에 직접 입력하세요 (run_openai.py 와 동일 포맷)
# ============================================================
EXPERIMENTS = [
    # {"pr": PR번호, "base": "base_sha", "head": "head_sha"},
    {"pr": 4, "base": "eeea83ef3ef89d2254ade380d3a9e03dd729e3e3", "head": "41c6719199b31502039d72e95e2230a52e5b1dec"},
    {"pr": 5, "base": "eeea83ef3ef89d2254ade380d3a9e03dd729e3e3", "head": "22f5de0651d559471bcb443be259c99bd8af2de9"},
    {"pr": 6, "base": "eeea83ef3ef89d2254ade380d3a9e03dd729e3e3", "head": "0c997db31004765fc4d68675cab5aac4c2ff5b70"},
    {"pr": 7, "base": "eeea83ef3ef89d2254ade380d3a9e03dd729e3e3", "head": "1190a3b96332a9a5060542f1c28ec5bf6b93804b"},
    {"pr": 8, "base": "eeea83ef3ef89d2254ade380d3a9e03dd729e3e3", "head": "a9a007244e03c0493c03f98b043c6d4ad10366f0"},
]
# ============================================================

# 모드별 기본 worker 수. sequential 은 service 가 worker 를 무시하므로 항상 1.
# hunk/pass 의 값은 --workers 로 덮어쓸 수 있다.
DEFAULT_HUNK_PASS_WORKERS = 3

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))


def _safe(model: str) -> str:
    """ollama 모델명(qwen3-coder:30b)을 파일명에 안전하게: ':' '/' → '-'."""
    return model.replace(":", "-").replace("/", "-")


# ============================================================
# GPU 샘플링 (nvidia-smi 서브프로세스, 의존성 없음)
# ============================================================
# 이 머신의 GPU 는 vGPU 프로파일(RTXA6000-48C)이라 power.draw / power.limit /
# temperature.gpu 가 모두 [N/A] 로 막혀 있다. 따라서 전력·에너지·온도 지표는
# 수집할 수 없고, 아래 필드만 유효하다:
#   utilization.gpu  SM(연산) 사용률 %        ← "GPU 점유율"의 핵심
#   utilization.memory  메모리 대역폭 사용률 %
#   memory.used      VRAM 사용량 MiB          (이 VM 안에선 ollama 가 유일 소비자)
#   clocks.sm        SM 클럭 MHz
_NVIDIA_SMI = shutil.which("nvidia-smi")
_GPU_QUERY = "utilization.gpu,utilization.memory,memory.used,memory.total,clocks.sm"


def _gpu_sample():
    """nvidia-smi 한 줄을 파싱해 한 샘플(dict)을 반환. 실패/미지원 시 None.
    숫자가 아닌 필드([N/A] 등)는 None 으로 둬서 집계에서 자연히 제외된다."""
    if not _NVIDIA_SMI:
        return None
    try:
        out = subprocess.run(
            [_NVIDIA_SMI, f"--query-gpu={_GPU_QUERY}",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip().splitlines()
    except Exception:
        return None
    if not out:
        return None

    def num(x):
        try:
            return float(x.strip())
        except (ValueError, AttributeError):
            return None

    parts = out[0].split(",")  # 단일 GPU 박스 → 첫 행
    if len(parts) < 5:
        return None
    return {
        "util_gpu": num(parts[0]),
        "util_mem": num(parts[1]),
        "mem_used": num(parts[2]),
        "mem_total": num(parts[3]),
        "clock_sm": num(parts[4]),
    }


class GpuSampler:
    """리뷰 1건이 도는 동안 백그라운드 스레드로 nvidia-smi 를 주기적으로 폴링.

    SM 사용률 / 메모리 대역폭 사용률 / VRAM 사용량을 누적해 mean·max·p95 로 요약한다.
    interval<=0 또는 nvidia-smi 부재 시 비활성(샘플 0개). context manager 로 사용:
        with GpuSampler(0.25) as s: ...; stats = s.summary()
    """

    def __init__(self, interval: float = 0.25):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._samples = []

    def __enter__(self):
        if _NVIDIA_SMI and self.interval > 0:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            s = _gpu_sample()
            if s:
                self._samples.append(s)
            self._stop.wait(self.interval)

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        return False

    def summary(self) -> dict:
        n = len(self._samples)
        if not n:
            return {"samples": 0, "available": bool(_NVIDIA_SMI and self.interval > 0)}

        def agg(key):
            vals = [s[key] for s in self._samples if s.get(key) is not None]
            if not vals:
                return None
            sv = sorted(vals)
            idx = int(round(0.95 * (len(sv) - 1)))
            return {"mean": round(sum(vals) / len(vals), 1),
                    "max": round(max(vals), 1),
                    "p95": round(sv[idx], 1)}

        return {
            "samples": n,
            "interval_s": self.interval,
            "util_gpu_pct": agg("util_gpu"),
            "util_mem_pct": agg("util_mem"),
            "mem_used_mib": agg("mem_used"),
            "mem_total_mib": self._samples[-1].get("mem_total"),
            "clock_sm_mhz": agg("clock_sm"),
        }


def _workers_for(mode: str, hunk_pass_workers: int) -> int:
    """모드별 worker 수. sequential 은 항상 1, hunk/pass 는 인자값."""
    return 1 if mode == "sequential" else max(1, hunk_pass_workers)


def run_one(pr: int, base: str, head: str, model: str,
            gpu_interval: float = 0.25) -> dict:
    from escargot_review_bot.config.config import EXPERIMENT_LOG_DIR
    from escargot_review_bot.domain.schemas import ReviewRequest
    from escargot_review_bot.service import generate_review_comments

    request = ReviewRequest(base_sha=base, head_sha=head, pull_request_number=pr)

    started = time.time()
    with GpuSampler(interval=gpu_interval) as sampler:
        comments = generate_review_comments(request)
    elapsed = round(time.time() - started, 1)
    gpu = sampler.summary()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(EXPERIMENT_LOG_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"PR-{pr}-{_safe(model)}-{ts}-comments.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(comments, f, ensure_ascii=False, indent=2)

    # GPU 요약은 별도 사이드카로(코멘트 JSON 오염 방지). 샘플이 있을 때만 기록.
    if gpu.get("samples"):
        gpu_path = out_dir / f"PR-{pr}-{_safe(model)}-{ts}-gpu.json"
        gpu_path.write_text(
            json.dumps({"pr": pr, "model": model, "elapsed_s": elapsed, **gpu},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")

    return {"comments": comments, "elapsed": elapsed, "out_path": out_path, "gpu": gpu}


def _gpu_oneline(gpu: dict) -> str:
    """run_one 의 gpu 요약을 한 줄 텍스트로. 샘플 없으면 빈 문자열."""
    if not gpu or not gpu.get("samples"):
        return ""
    u = gpu.get("util_gpu_pct") or {}
    m = gpu.get("mem_used_mib") or {}
    parts = []
    if u:
        parts.append(f"GPU {u['mean']}%/{u['max']}% (mean/max)")
    if m:
        parts.append(f"VRAM {m['max']:.0f}MiB peak")
    return "  |  ".join(parts)


def _run_single_mode(mode: str, model: str, workers: int, keep_alive: str,
                     results_out: str | None = None,
                     gpu_interval: float = 0.25,
                     run_number: str = "") -> int:
    """단일 모드를 현재 프로세스에서 실행. env 는 config import 전에 세팅해야 하므로
    여기서 service/config 를 처음 import 한다(run_one 안의 import 가 트리거).

    results_out 가 주어지면 PR별 결과(소요시간 포함)를 JSON 으로 기록한다.
    오케스트레이터(_orchestrate)가 이를 읽어 최종 요약에 합산한다."""
    # Ollama 강제 + 모드/worker/keep-alive 고정 (config import 전에 세팅).
    for key in ("LLM_PROVIDER", "MODEL_DEFECT", "MODEL_REFACTOR",
                "MODEL_COMPILER", "MODEL_STYLE", "MODEL_JUDGE"):
        os.environ[key] = "ollama" if key == "LLM_PROVIDER" else model
    os.environ["EXPERIMENT_LABEL"] = f"ollama-{_safe(model)}"
    if run_number:
        os.environ["LANGSMITH_RUN_PREFIX"] = run_number
    else:
        os.environ.pop("LANGSMITH_RUN_PREFIX", None)
    os.environ["REVIEW_PARALLELISM"] = mode
    os.environ["REVIEW_PARALLEL_WORKERS"] = str(workers)
    os.environ["OLLAMA_KEEP_ALIVE"] = keep_alive

    total = len(EXPERIMENTS)
    print(f"\n{'#' * 60}")
    print(f"[run_ollama] MODE = {mode}  workers = {workers}  keep_alive = {keep_alive}")
    print("#" * 60, flush=True)

    results = []
    for i, exp in enumerate(EXPERIMENTS, 1):
        pr, base, head = exp["pr"], exp["base"], exp["head"]
        print(f"\n[{mode}] [{i}/{total}] PR #{pr}  base={base}  head={head}")
        print("-" * 60)
        sys.stdout.flush()

        try:
            r = run_one(pr=pr, base=base, head=head, model=model,
                        gpu_interval=gpu_interval)
            comments = r["comments"]
            elapsed = r["elapsed"]
            out_path = r["out_path"]
            gpu = r.get("gpu") or {}

            print(f"  → {len(comments)}개 코멘트  |  {elapsed}s  |  {out_path.name}")
            gpu_line = _gpu_oneline(gpu)
            if gpu_line:
                print(f"     {gpu_line}")
            for j, c in enumerate(comments, 1):
                path = c.get("path", "?") if isinstance(c, dict) else c.path
                line = c.get("line", "?") if isinstance(c, dict) else c.line
                body = c.get("body", "") if isinstance(c, dict) else c.body
                print(f"     [{j}] {path}:{line}  {body[:120]}{'...' if len(body) > 120 else ''}")

            results.append({"mode": mode, "pr": pr, "status": "ok",
                            "comments": len(comments), "elapsed": elapsed,
                            "gpu_util_mean": (gpu.get("util_gpu_pct") or {}).get("mean"),
                            "gpu_util_max": (gpu.get("util_gpu_pct") or {}).get("max"),
                            "vram_peak_mib": (gpu.get("mem_used_mib") or {}).get("max")})

        except KeyboardInterrupt:
            print("\n[run_ollama] 중단됨 (KeyboardInterrupt)")
            break
        except Exception as e:
            print(f"  ! PR #{pr} 실패: {e}")
            results.append({"mode": mode, "pr": pr, "status": "failed", "error": str(e)})

        sys.stdout.flush()

    print(f"\n{'=' * 60}")
    print(f"[run_ollama] MODE={mode} 완료  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    for r in results:
        if r["status"] == "ok":
            g = ""
            if r.get("gpu_util_mean") is not None:
                g = f"  GPU {r['gpu_util_mean']}%/{r['gpu_util_max']}%"
            print(f"  [{r['mode']:>10}] PR #{r['pr']:>4}  ok  {r['comments']}개 코멘트  {r['elapsed']}s{g}")
        else:
            print(f"  [{r['mode']:>10}] PR #{r['pr']:>4}  FAILED  {r.get('error', '')}")
    print("=" * 60)

    if results_out:
        try:
            Path(results_out).write_text(
                json.dumps(results, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[run_ollama] 결과 파일 기록 실패({results_out}): {e}", file=sys.stderr)

    return 0 if all(r["status"] == "ok" for r in results) else 1


def _orchestrate(modes: list, model: str, hunk_pass_workers: int, keep_alive: str,
                 gpu_interval: float = 0.25, run_number: str = "") -> int:
    """여러 모드 요청 시: 모드마다 자기 자신을 단일 모드 서브프로세스로 재실행.
    config 모듈 상수(REVIEW_PARALLELISM/WORKERS/KEEP_ALIVE)는 import 시 고정되므로
    한 프로세스에서 모드를 바꿀 수 없어 모드별 새 프로세스가 필요하다."""
    total = len(EXPERIMENTS)
    print(f"[run_ollama] 총 {total}개 PR × {len(modes)}개 모드 실험 시작  "
          f"model={model}  modes={','.join(modes)}  hunk/pass workers={hunk_pass_workers}")
    print(f"[run_ollama] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60, flush=True)

    overall_start = time.time()
    per_mode = []  # [{"mode","rc","wall","results":[...]}]
    for mode in modes:
        results_path = Path(tempfile.gettempdir()) / f"run_ollama_{mode}_{os.getpid()}.json"
        cmd = [sys.executable, str(Path(__file__).resolve()),
               "--model", model, "--modes", mode,
               "--workers", str(hunk_pass_workers), "--keep-alive", keep_alive,
               "--gpu-interval", str(gpu_interval),
               "--results-out", str(results_path)]
        if run_number:
            cmd += ["--run-number", run_number]
        print(f"\n[run_ollama] ▶ subprocess: {' '.join(cmd)}", flush=True)
        started = time.time()
        try:
            proc = subprocess.run(cmd, cwd=str(_ROOT))
        except KeyboardInterrupt:
            print("\n[run_ollama] 중단됨 (KeyboardInterrupt)")
            break
        wall = round(time.time() - started, 1)

        pr_results = []
        if results_path.exists():
            try:
                pr_results = json.loads(results_path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[run_ollama] 결과 파일 읽기 실패({results_path}): {e}", file=sys.stderr)
            finally:
                results_path.unlink(missing_ok=True)

        per_mode.append({"mode": mode, "rc": proc.returncode, "wall": wall, "results": pr_results})
        print(f"[run_ollama] ◀ mode={mode} rc={proc.returncode}  wall={wall}s", flush=True)

    overall = round(time.time() - overall_start, 1)

    # ---- 최종 소요 시간 요약 (모드별 각 PR + 모드 소계 + 전체) ----
    print(f"\n{'=' * 60}")
    print(f"[run_ollama] 전체 완료  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print("소요 시간 요약 (모드별 PR / 모드 소계 / 전체)")
    print("-" * 60)
    for m in per_mode:
        status = "ok" if m["rc"] == 0 else f"FAILED(rc={m['rc']})"
        pr_sum = round(sum(r.get("elapsed", 0) for r in m["results"]), 1)
        print(f"\n[{m['mode']:>10}]  {status}  (wall {m['wall']}s)")
        for r in m["results"]:
            if r["status"] == "ok":
                g = ""
                if r.get("gpu_util_mean") is not None:
                    g = f"  |  GPU {r['gpu_util_mean']}%/{r['gpu_util_max']}%"
                print(f"     PR #{r['pr']:>4}  {r['elapsed']:>7}s  ({r['comments']}개 코멘트){g}")
            else:
                print(f"     PR #{r['pr']:>4}  FAILED  {r.get('error', '')}")
        print(f"     {'─' * 40}")
        # 모드 GPU 소계: PR별 mean 의 평균, max 중 최대, VRAM peak 중 최대
        umeans = [r["gpu_util_mean"] for r in m["results"]
                  if r.get("gpu_util_mean") is not None]
        umaxes = [r["gpu_util_max"] for r in m["results"]
                  if r.get("gpu_util_max") is not None]
        vpeaks = [r["vram_peak_mib"] for r in m["results"]
                  if r.get("vram_peak_mib") is not None]
        gpu_sub = ""
        if umeans:
            gpu_sub = (f"  |  GPU 평균 {round(sum(umeans)/len(umeans), 1)}% "
                       f"(peak {max(umaxes)}%)  VRAM {max(vpeaks):.0f}MiB")
        print(f"     소계: 리뷰합 {pr_sum}s  |  wall {m['wall']}s{gpu_sub}")
    print("-" * 60)
    print(f"전체 소요 시간: {overall}s")
    print("=" * 60)

    return 0 if all(m["rc"] == 0 for m in per_mode) and len(per_mode) == len(modes) else 1


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument(
        "--model", default="qwen3-coder:30b",
        help="Ollama model (default: qwen3-coder:30b). 로컬에 pull 돼 있어야 함.",
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=("sequential", "hunk", "pass"),
        default=["sequential", "hunk", "pass"],
        metavar="MODE",
        help="REVIEW_PARALLELISM 실행 모드 (default: 3가지 모두). 모드마다 별도 "
             "서브프로세스로 전체 PR 을 1회씩 돈다.",
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_HUNK_PASS_WORKERS,
        help=f"hunk/pass 모드의 헝크풀 worker 수 (default: {DEFAULT_HUNK_PASS_WORKERS}). "
             "sequential 은 항상 1.",
    )
    parser.add_argument(
        "--keep-alive", default="30m",
        help="OLLAMA_KEEP_ALIVE (default: 30m). '0' 이면 service 가드가 hunk→1, "
             "pass→sequential 로 클램프하므로 worker 동시성을 쓰려면 0 이외 값으로 둘 것.",
    )
    parser.add_argument(
        "--gpu-interval", type=float, default=0.25,
        help="GPU 샘플링 주기(초, default: 0.25). 0 이면 GPU 샘플링 끔. "
             "nvidia-smi 가 없으면 자동 비활성. vGPU(RTXA6000-48C)라 전력/온도는 [N/A].",
    )
    parser.add_argument(
        "--run-number", default="",
        help="LangSmith 프로젝트명 앞에 붙을 식별 번호/문자열 (예: 01, exp02). "
             "미지정 시 prefix 없음.",
    )
    parser.add_argument(
        "--results-out", default=None,
        help=argparse.SUPPRESS,  # 내부용: 오케스트레이터가 단일 모드 자식에 PR별 결과 기록을 요청
    )
    args = parser.parse_args()

    # 여러 모드 → 모드별 서브프로세스 오케스트레이션.
    # 단일 모드 → 현재 프로세스에서 직접 실행(config 가 이 모드/worker 로 고정됨).
    if len(args.modes) > 1:
        return _orchestrate(args.modes, args.model, max(1, args.workers),
                            args.keep_alive, args.gpu_interval, args.run_number)

    mode = args.modes[0]
    workers = _workers_for(mode, args.workers)
    return _run_single_mode(mode, args.model, workers, args.keep_alive,
                            args.results_out, args.gpu_interval, args.run_number)


if __name__ == "__main__":
    sys.exit(main())
