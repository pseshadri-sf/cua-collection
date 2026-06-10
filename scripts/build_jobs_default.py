"""Build a jobs JSONL using the per-app OPTIMAL planner config (validated on 100 assets).

Per-app defaults (cost-optimized, quality-preserving):
  FreeCAD -> gemini-3.1-flash-lite-preview + low reasoning  (~$0.005/asset, -2.8% match, 94% cheaper)
  Blender -> gemini-3.1-pro-preview        + low reasoning  (flash-lite halves BL assembly fidelity)
Both: qwen3-vl-30b executor, compositional, grounded, low SLM reasoning, multi-view goal, --postprocess.
For max Blender quality add --best-of-both to BL (Chamfer +40%, ~3x planner cost).

Usage:
  uv run python scripts/build_jobs_default.py --job-ids ids.json --out jobs.jsonl [--bl-best-of-both]
or import build_extra_args(app) elsewhere.
"""
import argparse
import json

FC_PLANNER = "google/gemini-3.1-flash-lite-preview"
BL_PLANNER = "google/gemini-3.1-pro-preview"
EXECUTOR = "qwen/qwen3-vl-30b-a3b-instruct"


def build_extra_args(app: str, bl_best_of_both: bool = False) -> list[str]:
    app = "blender" if app.startswith("bl") or app == "blender" else "freecad"
    planner = BL_PLANNER if app == "blender" else FC_PLANNER
    a = ["--model", EXECUTOR, "--reasoning-effort", "low", "--image-max-dim", "1024",
         "--grounded", "--planner-model", planner, "--compositional",
         "--planner-reasoning", "low", "--postprocess"]
    if app == "blender" and bl_best_of_both:
        a.append("--best-of-both")
    return a


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-jobs", action="append", required=True,
                    help="existing jobs JSONL(s) providing job_id/app/goal_path/asset_path")
    ap.add_argument("--job-ids", help="optional JSON list of job_ids to include (default: all)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--bl-best-of-both", action="store_true")
    ap.add_argument("--max-steps", type=int, default=80)
    ap.add_argument("--timeout-sec", type=int, default=1200)
    args = ap.parse_args()

    src = {}
    for sj in args.source_jobs:
        for line in open(sj):
            line = line.strip()
            if line:
                j = json.loads(line); src[j["job_id"]] = j
    ids = json.load(open(args.job_ids)) if args.job_ids else list(src)
    n = 0
    with open(args.out, "w") as f:
        for jid in ids:
            j = src.get(jid)
            if not j:
                continue
            jj = dict(j)
            jj["extra_args"] = build_extra_args(j["app"], args.bl_best_of_both)
            jj["max_steps"] = args.max_steps
            jj["timeout_sec"] = args.timeout_sec
            f.write(json.dumps(jj) + "\n"); n += 1
    print(f"wrote {n} jobs -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
