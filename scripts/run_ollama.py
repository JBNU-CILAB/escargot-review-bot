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

결과 파일 (experiments/ 디렉토리)
---------------------------------
    PR-{n}-{model}-{ts}-comments.json  : GitHub에 올라갈 코멘트 원본 (모델명의 ':' 은 '-' 로 치환)
    PR-{n}-ollama-{model}-{ts}.jsonl   : metrics (토큰수, latency 등)

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
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))


def _safe(model: str) -> str:
    """ollama 모델명(qwen3-coder:30b)을 파일명에 안전하게: ':' '/' → '-'."""
    return model.replace(":", "-").replace("/", "-")


def _workers_for(mode: str, hunk_pass_workers: int) -> int:
    """모드별 worker 수. sequential 은 항상 1, hunk/pass 는 인자값."""
    return 1 if mode == "sequential" else max(1, hunk_pass_workers)


def run_one(pr: int, base: str, head: str, model: str) -> dict:
    from escargot_review_bot.config.config import EXPERIMENT_LOG_DIR
    from escargot_review_bot.domain.schemas import ReviewRequest
    from escargot_review_bot.service import generate_review_comments

    request = ReviewRequest(base_sha=base, head_sha=head, pull_request_number=pr)

    started = time.time()
    comments = generate_review_comments(request)
    elapsed = round(time.time() - started, 1)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(EXPERIMENT_LOG_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"PR-{pr}-{_safe(model)}-{ts}-comments.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(comments, f, ensure_ascii=False, indent=2)

    return {"comments": comments, "elapsed": elapsed, "out_path": out_path}


def _run_single_mode(mode: str, model: str, workers: int, keep_alive: str,
                     results_out: str | None = None) -> int:
    """단일 모드를 현재 프로세스에서 실행. env 는 config import 전에 세팅해야 하므로
    여기서 service/config 를 처음 import 한다(run_one 안의 import 가 트리거).

    results_out 가 주어지면 PR별 결과(소요시간 포함)를 JSON 으로 기록한다.
    오케스트레이터(_orchestrate)가 이를 읽어 최종 요약에 합산한다."""
    # Ollama 강제 + 모드/worker/keep-alive 고정 (config import 전에 세팅).
    for key in ("LLM_PROVIDER", "MODEL_DEFECT", "MODEL_REFACTOR",
                "MODEL_COMPILER", "MODEL_STYLE", "MODEL_JUDGE"):
        os.environ[key] = "ollama" if key == "LLM_PROVIDER" else model
    os.environ["EXPERIMENT_LABEL"] = f"ollama-{_safe(model)}"
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
            r = run_one(pr=pr, base=base, head=head, model=model)
            comments = r["comments"]
            elapsed = r["elapsed"]
            out_path = r["out_path"]

            print(f"  → {len(comments)}개 코멘트  |  {elapsed}s  |  {out_path.name}")
            for j, c in enumerate(comments, 1):
                path = c.get("path", "?") if isinstance(c, dict) else c.path
                line = c.get("line", "?") if isinstance(c, dict) else c.line
                body = c.get("body", "") if isinstance(c, dict) else c.body
                print(f"     [{j}] {path}:{line}  {body[:120]}{'...' if len(body) > 120 else ''}")

            results.append({"mode": mode, "pr": pr, "status": "ok",
                            "comments": len(comments), "elapsed": elapsed})

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
            print(f"  [{r['mode']:>10}] PR #{r['pr']:>4}  ok  {r['comments']}개 코멘트  {r['elapsed']}s")
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


def _orchestrate(modes: list, model: str, hunk_pass_workers: int, keep_alive: str) -> int:
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
               "--results-out", str(results_path)]
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
                print(f"     PR #{r['pr']:>4}  {r['elapsed']:>7}s  ({r['comments']}개 코멘트)")
            else:
                print(f"     PR #{r['pr']:>4}  FAILED  {r.get('error', '')}")
        print(f"     {'─' * 40}")
        print(f"     소계: 리뷰합 {pr_sum}s  |  wall {m['wall']}s")
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
        "--results-out", default=None,
        help=argparse.SUPPRESS,  # 내부용: 오케스트레이터가 단일 모드 자식에 PR별 결과 기록을 요청
    )
    args = parser.parse_args()

    # 여러 모드 → 모드별 서브프로세스 오케스트레이션.
    # 단일 모드 → 현재 프로세스에서 직접 실행(config 가 이 모드/worker 로 고정됨).
    if len(args.modes) > 1:
        return _orchestrate(args.modes, args.model, max(1, args.workers), args.keep_alive)

    mode = args.modes[0]
    workers = _workers_for(mode, args.workers)
    return _run_single_mode(mode, args.model, workers, args.keep_alive, args.results_out)


if __name__ == "__main__":
    sys.exit(main())
