"""OpenAI 실험 배치 실행 스크립트 (SSH 연결 종료 후에도 지속 실행)

실행 방법
---------
    # 포그라운드 (터미널에서 직접)
    python scripts/run_openai.py

    # 백그라운드 (SSH 끊겨도 계속 실행)
    nohup python scripts/run_openai.py > experiments/run.log 2>&1 &
    tail -f experiments/run.log   # 로그 확인

    # 모델 변경
    python scripts/run_openai.py --model gpt-4o-mini

결과 파일 (experiments/ 디렉토리)
---------------------------------
    PR-{n}-{model}-{ts}-comments.json  : GitHub에 올라갈 코멘트 원본
    PR-{n}-openai-{model}-{ts}.jsonl   : metrics (토큰수, latency 등)
"""

# ============================================================
# 실험 대상 PR 목록 — 여기에 직접 입력하세요
# ============================================================
EXPERIMENTS = [
    # {"pr": PR번호, "base": "base_sha", "head": "head_sha"},
    {"pr": 4, "base": "eeea83ef3ef89d2254ade380d3a9e03dd729e3e3", "head": "41c6719199b31502039d72e95e2230a52e5b1dec"},
    # {"pr": 5, "base": "eeea83ef3ef89d2254ade380d3a9e03dd729e3e3", "head": "22f5de0651d559471bcb443be259c99bd8af2de9"},
    # {"pr": 6, "base": "eeea83ef3ef89d2254ade380d3a9e03dd729e3e3", "head": "d0c997db31004765fc4d68675cab5aac4c2ff5b70dd333"},
    # {"pr": 7, "base": "eeea83ef3ef89d2254ade380d3a9e03dd729e3e3", "head": "1190a3b96332a9a5060542f1c28ec5bf6b93804b"},
    # {"pr": 8, "base": "eeea83ef3ef89d2254ade380d3a9e03dd729e3e3", "head": "a9a007244e03c0493c03f98b043c6d4ad10366f0"},
]
# ============================================================

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))


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
    out_path = out_dir / f"PR-{pr}-{model}-{ts}-comments.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(comments, f, ensure_ascii=False, indent=2)

    return {"comments": comments, "elapsed": elapsed, "out_path": out_path}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-4o", help="OpenAI model (default: gpt-4o)")
    args = parser.parse_args()

    # OpenAI 강제 설정
    for key in ("LLM_PROVIDER", "MODEL_DEFECT", "MODEL_REFACTOR",
                "MODEL_COMPILER", "MODEL_STYLE", "MODEL_JUDGE"):
        os.environ[key] = "openai" if key == "LLM_PROVIDER" else args.model
    os.environ["EXPERIMENT_LABEL"] = f"openai-{args.model}"
    os.environ["REVIEW_PARALLEL_WORKERS"] = "1"  # rate limit 방지

    total = len(EXPERIMENTS)
    print(f"[run_openai] 총 {total}개 PR 실험 시작  model={args.model}")
    print(f"[run_openai] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    results = []
    for i, exp in enumerate(EXPERIMENTS, 1):
        pr, base, head = exp["pr"], exp["base"], exp["head"]
        print(f"\n[{i}/{total}] PR #{pr}  base={base}  head={head}")
        print("-" * 60)
        sys.stdout.flush()

        try:
            r = run_one(pr=pr, base=base, head=head, model=args.model)
            comments = r["comments"]
            elapsed = r["elapsed"]
            out_path = r["out_path"]

            print(f"  → {len(comments)}개 코멘트  |  {elapsed}s  |  {out_path.name}")
            for j, c in enumerate(comments, 1):
                path = c.get("path", "?") if isinstance(c, dict) else c.path
                line = c.get("line", "?") if isinstance(c, dict) else c.line
                body = c.get("body", "") if isinstance(c, dict) else c.body
                print(f"     [{j}] {path}:{line}  {body[:120]}{'...' if len(body) > 120 else ''}")

            results.append({"pr": pr, "status": "ok", "comments": len(comments), "elapsed": elapsed})

        except KeyboardInterrupt:
            print("\n[run_openai] 중단됨 (KeyboardInterrupt)")
            break
        except Exception as e:
            print(f"  ! PR #{pr} 실패: {e}")
            results.append({"pr": pr, "status": "failed", "error": str(e)})

        sys.stdout.flush()

    print(f"\n{'=' * 60}")
    print(f"[run_openai] 완료  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    for r in results:
        status = r["status"]
        if status == "ok":
            print(f"  PR #{r['pr']:>4}  ok  {r['comments']}개 코멘트  {r['elapsed']}s")
        else:
            print(f"  PR #{r['pr']:>4}  FAILED  {r.get('error', '')}")
    print("=" * 60)

    return 0 if all(r["status"] == "ok" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
