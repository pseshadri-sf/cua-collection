"""Headless planner ablation for the KiCad track.

For each board x each planner model: call the frontier planner (grounded on the
goal atlas + sidecar) to get a pcbnew `full_code` build, reconstruct it onto the
blank seed board headlessly, and score it against the goal with compare_kicad.
No GUI / Xvfb — plan quality lives in the generated code, so this isolates the
planner cheaply (the GUI replay is planner-agnostic).

Outputs a JSONL row per (board, planner) with match_score, per-component
sub-scores, footprint counts, and planner token usage. Aggregates printed at the
end (mean match_score + mean output tokens per planner).

Usage:
  uv run python scripts/kicad_plan_ablation.py \
      --manifest ~/cua_kicad_smoketest/manifest.jsonl \
      --planners google/gemini-3-flash-preview,google/gemini-3.1-flash-lite-preview,google/gemini-3.1-pro-preview \
      --limit 25 --out ~/cua_kicad_smoketest/ablation.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from cua_smoketest.agent.frontier_planner import FrontierPlanner  # noqa: E402
from cua_smoketest.agent.vlm_client import _extract_goal_name      # noqa: E402
from cua_smoketest.agent.kicad_eval import evaluate_kicad_run      # noqa: E402

sys.path.insert(0, str(_REPO / "scripts"))
from render_kicad_goal import render_goal                          # noqa: E402


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
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--planners", required=True, help="comma-separated OpenRouter model ids")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--reasoning", default="low")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--env-file", type=Path, default=Path("/home/ubuntu/.env"))
    p.add_argument("--api-key-var", default="OPENAI_API_KEY")
    args = p.parse_args(argv)

    _load_env(args.env_file)
    api_key = os.environ.get(args.api_key_var, "")
    if not api_key:
        print(f"ERROR: {args.api_key_var} not set", file=sys.stderr); return 2

    planners = [m.strip() for m in args.planners.split(",") if m.strip()]
    boards = []
    for line in args.manifest.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        j = json.loads(line)
        b = j.get("staged_asset")
        if b and Path(b).exists():
            boards.append((Path(b), j))
    boards = boards[: args.limit]
    print(f"[ablation] {len(boards)} boards x {len(planners)} planners", flush=True)

    out_f = open(args.out, "w")
    agg: dict[str, list] = {m: [] for m in planners}
    cost: dict[str, list] = {m: [] for m in planners}
    for bi, (board, meta) in enumerate(boards):
        goal_png = render_goal(board)  # cached atlas + sidecar
        if not goal_png:
            print(f"[ablation] [{bi}] render failed for {board.name}", flush=True)
            continue
        gname = _extract_goal_name(goal_png)

        def _eval_planner(model: str) -> dict:
            row = {"board": board.name, "repo": meta.get("repo"),
                   "total_components": meta.get("total_components"),
                   "planner": model, "match_score": None, "tokens_out": None}
            try:
                planner = FrontierPlanner(api_key=api_key, model=model, app="kicad",
                                          reasoning_effort=args.reasoning, compositional=False)
                plan = planner.plan(goal_png, goal_name=gname)
            except Exception as exc:  # noqa: BLE001
                print(f"[ablation] [{bi}] {model} plan error: {exc}", flush=True)
                return row
            if plan and plan.get("full_code"):
                with tempfile.TemporaryDirectory() as td:
                    traj = Path(td) / "t.json"
                    traj.write_text(json.dumps({"app": "kicad", "trajectory": [
                        {"step_idx": 1, "action": {"type": "pcbnew_eval",
                         "code": plan["full_code"]}}]}))
                    ev = evaluate_kicad_run(traj, board, Path(td) / "eval")
                s = ev["score"]
                u = plan.get("_planner_usage") or {}
                row.update({"match_score": s["match_score"],
                            "placement": s["footprint_placement"],
                            "nets": s["connectivity_nets"], "outline": s["outline_match"],
                            "counts": s["count_agreement"], "matched": s["matched_footprints"],
                            "goal_fp": s["goal_footprints"], "agent_fp": s["agent_footprints"],
                            "tokens_out": u.get("completion_tokens")})
            return row

        # The 3 planner calls are independent I/O — run them concurrently.
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
        with ThreadPoolExecutor(max_workers=len(planners)) as ex:
            rows = list(ex.map(_eval_planner, planners))
        for row in rows:
            if row["match_score"] is not None:
                agg[row["planner"]].append(row["match_score"])
                if row.get("tokens_out"):
                    cost[row["planner"]].append(row["tokens_out"])
            out_f.write(json.dumps(row) + "\n"); out_f.flush()
        if (bi + 1) % 5 == 0:
            print(f"[ablation] {bi+1}/{len(boards)} boards done", flush=True)
    out_f.close()

    print("\n==== ABLATION SUMMARY ====")
    print(f"{'planner':<42}{'n':>4}{'mean_score':>12}{'mean_tok_out':>14}")
    for m in planners:
        scores = agg[m]; toks = cost[m]
        ms = sum(scores) / len(scores) if scores else 0.0
        mt = sum(toks) / len(toks) if toks else 0.0
        print(f"{m:<42}{len(scores):>4}{ms:>12.1f}{mt:>14.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
