"""Walk a batch dir, score every run by reconstructing the agent's model
and diffing geometry against the original goal asset.

The original goal screenshot name (needed to resolve back to the source
.step/.FCStd/.blend file) is read from batch_summary.csv's `goal` column.
The run dir only has a generic `goal.png` copy.

Output:
  <batch_dir>/batch_evaluation.csv
  <batch_dir>/<run>/eval/{agent_model.{FCStd,blend}, agent_stats.json,
                          goal_stats.json, eval.json}
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cua_smoketest.agent.evaluator import (  # noqa: E402
    evaluate_freecad_run, evaluate_blender_run,
    resolve_freecad_goal_asset, resolve_blender_goal_asset,
)


def _detect_app(run_dir: Path) -> str | None:
    name = run_dir.name
    if name.startswith("fc_"):
        return "freecad"
    if name.startswith("bl_"):
        return "blender"
    # Fallback: read trajectory.json model field or peek at goal name.
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("batch_dir", type=Path)
    p.add_argument("--include", action="append", default=None,
                   help="Only score these run-name prefixes (repeatable).")
    args = p.parse_args(argv)

    # Load original goal-screenshot filenames from batch_summary.csv.
    summary_csv = args.batch_dir / "batch_summary.csv"
    original_goal_by_run: dict[str, str] = {}
    if summary_csv.exists():
        with open(summary_csv) as fh:
            r = csv.DictReader(fh)
            for row in r:
                original_goal_by_run[row["run_name"]] = row["goal"]

    rows: list[dict] = []
    for run_dir in sorted(args.batch_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        if args.include and not any(run_dir.name.startswith(p) for p in args.include):
            continue
        traj = run_dir / "trajectory.json"
        if not traj.exists():
            continue
        app = _detect_app(run_dir)
        if app is None:
            print(f"[skip] {run_dir.name}: unknown app prefix")
            continue
        original_goal = original_goal_by_run.get(run_dir.name)
        if not original_goal:
            print(f"[skip] {run_dir.name}: no original goal in summary CSV")
            continue
        resolve = resolve_freecad_goal_asset if app == "freecad" else resolve_blender_goal_asset
        goal_asset = resolve(Path(original_goal))
        if goal_asset is None:
            print(f"[skip] {run_dir.name}: goal asset not resolved")
            continue
        eval_dir = run_dir / "eval"
        print(f"[eval] {run_dir.name}  goal_asset={goal_asset.name}")
        try:
            if app == "freecad":
                result = evaluate_freecad_run(traj, goal_asset, eval_dir)
            else:
                result = evaluate_blender_run(traj, goal_asset, eval_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"[err]  {run_dir.name}: {exc}")
            continue
        (eval_dir / "eval.json").write_text(json.dumps(result, indent=2))
        row = {
            "run": run_dir.name,
            "app": app,
            "goal_asset": goal_asset.name,
            "chunks": result["chunks_count"],
            "agent_found": result["agent_stats"]["found"],
            "goal_found": result["goal_stats"]["found"],
            "agent_objects": result["agent_stats"]["object_count"],
            "goal_objects": result["goal_stats"]["object_count"],
            "agent_vol": round(result["agent_stats"]["volume"], 2),
            "goal_vol": round(result["goal_stats"]["volume"], 2),
            **{f"score_{k}": v for k, v in result["score"].items()},
        }
        rows.append(row)
        print(f"       match_score={result['score']['match_score']}  "
              f"vol_ratio={result['score']['vol_ratio']}  "
              f"bbox_score={result['score']['bbox_score']}")

    out_csv = args.batch_dir / "batch_evaluation.csv"
    if rows:
        with open(out_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nwrote {out_csv} ({len(rows)} rows)")
    else:
        print("no runs scored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
