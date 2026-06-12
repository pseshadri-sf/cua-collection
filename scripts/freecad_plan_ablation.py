"""Headless planner ablation for the FreeCAD track.

For each asset x each planner model: call the frontier planner (grounded on the
goal atlas + sidecar) to get the Part-API `full_code` build, reconstruct it via
freecad_reconstruct.py (headless freecadcmd), measure the resulting doc, and
score it against the goal stats with `compare`. No GUI / Xvfb — plan quality
lives in the generated code, so this isolates the planner cheaply (the GUI
replay is planner-agnostic).

Per-asset planners run concurrently (independent API I/O). Per-asset
reconstructions are serialised inside `evaluate_freecad_run` (freecadcmd cost).

Outputs a JSONL row per (asset, planner) with match_score, per-component
sub-scores, planner token usage. Aggregates printed at the end.

Usage:
  uv run python scripts/freecad_plan_ablation.py \
      --sample ~/cua_kicad_smoketest/fc_sample.jsonl \
      --planners "google/gemini-3.1-flash-lite-preview,google/gemini-3-flash-preview,google/gemini-3.1-pro-preview" \
      --out ~/cua_gui_smoketest/fc_ablation.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from cua_smoketest.agent.frontier_planner import FrontierPlanner  # noqa: E402
from cua_smoketest.agent.vlm_client import _extract_goal_name      # noqa: E402
from cua_smoketest.agent.evaluator import (                        # noqa: E402
    evaluate_freecad_run, resolve_freecad_goal_asset,
)


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--sample", type=Path, required=True,
                   help="JSONL of {asset_path, goal_path} entries to evaluate")
    p.add_argument("--planners", required=True, help="comma-separated OpenRouter model ids")
    p.add_argument("--reasoning", default="low")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--env-file", type=Path, default=Path("/home/ubuntu/.env"))
    p.add_argument("--api-key-var", default="OPENAI_API_KEY")
    p.add_argument("--workers", type=int, default=4,
                   help="how many assets to process in parallel (planners run "
                        "concurrently within each asset)")
    args = p.parse_args(argv)

    _load_env(args.env_file)
    api_key = os.environ.get(args.api_key_var, "")
    if not api_key:
        print(f"ERROR: {args.api_key_var} not set", file=sys.stderr); return 2

    planners = [m.strip() for m in args.planners.split(",") if m.strip()]
    samples = [json.loads(l) for l in args.sample.read_text().splitlines() if l.strip()]
    print(f"[ablation] {len(samples)} assets x {len(planners)} planners", flush=True)

    out_f = open(args.out, "w")
    agg: dict[str, list] = {m: [] for m in planners}
    cost: dict[str, list] = {m: [] for m in planners}
    lock = __import__("threading").Lock()

    def _eval_planner(sample: dict, model: str) -> dict:
        asset_path = sample["asset_path"]
        goal_path = sample["goal_path"]
        row = {"asset": os.path.basename(asset_path), "planner": model,
               "match_score": None, "tokens_out": None,
               "goal_face_count": sample.get("face_count")}
        try:
            planner = FrontierPlanner(api_key=api_key, model=model, app="freecad",
                                      reasoning_effort=args.reasoning, compositional=False)
            plan = planner.plan(Path(goal_path), goal_name=_extract_goal_name(Path(goal_path)))
        except Exception as exc:  # noqa: BLE001
            print(f"[ablation] {os.path.basename(asset_path)[:50]:<52} {model.split('/')[-1]:<30} plan err: {exc}", flush=True)
            return row
        if not (plan and plan.get("full_code")):
            return row
        u = plan.get("_planner_usage") or {}
        row["tokens_out"] = u.get("completion_tokens")
        row["tokens_in"] = u.get("prompt_tokens")
        # Reconstruct headlessly via the full_code as a single chunk: wrap into
        # a fake trajectory and call evaluate_freecad_run.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            traj = tdp / "t.json"
            traj.write_text(json.dumps({"app": "freecad", "trajectory": [
                {"step_idx": 1, "action": {"type": "python_eval",
                 "code": plan["full_code"]}}]}))
            try:
                ev = evaluate_freecad_run(traj, Path(asset_path), tdp / "eval")
            except Exception as exc:  # noqa: BLE001
                print(f"[ablation] {os.path.basename(asset_path)[:50]:<52} {model.split('/')[-1]:<30} recon err: {exc}", flush=True)
                return row
        s = ev.get("score") or {}
        row.update({
            "match_score": s.get("match_score"),
            "volume_score": s.get("volume_score"),
            "bbox_score": s.get("bbox_score"),
            "shape_proportions": s.get("shape_proportions"),
            "name_score": s.get("name_score"),
            "agent_objects": (ev.get("agent_stats") or {}).get("object_count"),
            "goal_objects": (ev.get("goal_stats") or {}).get("object_count"),
        })
        return row

    def _eval_asset(i: int, sample: dict) -> list[dict]:
        # planners for this asset, in parallel
        with ThreadPoolExecutor(max_workers=len(planners)) as ex:
            rows = list(ex.map(lambda m: _eval_planner(sample, m), planners))
        with lock:
            for r in rows:
                if r["match_score"] is not None:
                    agg[r["planner"]].append(r["match_score"])
                    if r.get("tokens_out"):
                        cost[r["planner"]].append(r["tokens_out"])
                out_f.write(json.dumps(r) + "\n")
            out_f.flush()
        if (i + 1) % 5 == 0:
            print(f"[ablation] {i+1}/{len(samples)} assets done", flush=True)
        return rows

    # assets in parallel
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(lambda kv: _eval_asset(*kv), enumerate(samples)))
    out_f.close()

    print("\n==================== ABLATION SUMMARY ====================")
    print(f"  {'planner':<42}{'n':>4}{'mean':>8}{'median':>8}{'tok_out':>10}")
    import statistics as st
    for m in planners:
        scores = agg[m]; toks = cost[m]
        mn = st.mean(scores) if scores else 0.0
        md = st.median(scores) if scores else 0.0
        mt = st.mean(toks) if toks else 0.0
        print(f"  {m:<42}{len(scores):>4}{mn:>8.1f}{md:>8.1f}{mt:>10.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
