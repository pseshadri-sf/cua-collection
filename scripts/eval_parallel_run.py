"""Run the geometric evaluator on every trajectory in a parallel-orchestrator
output dir, producing `<job>/eval/eval.json`. The orchestrator only does this
when --best-of > 1; this script catches the default case.

Job → app dispatch:
  - Prefer the `app` field from the jobs-file (passed via --jobs-file).
  - Fallback: job_id prefix `b_` / `bl_` → blender, else freecad.
  - Override: if goal_path contains "blender_smoketest", force blender.
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cua_smoketest.agent.evaluator import (   # noqa: E402
    evaluate_freecad_run, evaluate_blender_run, evaluate_kicad_run,
    resolve_freecad_goal_asset, resolve_blender_goal_asset, resolve_kicad_goal_asset,
)

_RESOLVERS = {"freecad": resolve_freecad_goal_asset,
              "blender": resolve_blender_goal_asset,
              "kicad": resolve_kicad_goal_asset}
_RUNNERS = {"freecad": evaluate_freecad_run,
            "blender": evaluate_blender_run,
            "kicad": evaluate_kicad_run}


def _route(job_id: str, goal_path: str, declared_app: str | None) -> str:
    if "kicad_smoketest" in goal_path:
        return "kicad"
    if "blender_smoketest" in goal_path:
        return "blender"
    if declared_app in ("freecad", "blender", "kicad"):
        return declared_app
    if job_id.startswith(("b", "bl_")):
        return "blender"
    return "freecad"


def _eval_one(traj_path_str: str, app: str, goal_path: str, force: bool) -> dict:
    traj = Path(traj_path_str)
    edir = traj.parent / "eval"
    ej = edir / "eval.json"
    if ej.exists() and not force:
        return {"job": traj.parent.name, "status": "skipped",
                "match_score": json.loads(ej.read_text())["score"]["match_score"]}
    resolver = _RESOLVERS.get(app, resolve_freecad_goal_asset)
    runner   = _RUNNERS.get(app, evaluate_freecad_run)
    asset = resolver(Path(goal_path))
    if not asset or not asset.exists():
        return {"job": traj.parent.name, "status": "no_asset", "match_score": None}
    try:
        ev = runner(traj, asset, edir)
        ej.write_text(json.dumps(ev, indent=2))
        return {"job": traj.parent.name, "status": "ok",
                "match_score": ev["score"]["match_score"], "app": app}
    except Exception as exc:    # noqa: BLE001
        return {"job": traj.parent.name, "status": "err",
                "error": f"{type(exc).__name__}: {exc}", "match_score": None}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run_dir", type=Path)
    p.add_argument("--jobs-file", type=Path, default=None,
                   help="Optional JSONL with per-job app/goal_path. Falls back to "
                        "summary.json + heuristics.")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--force", action="store_true")
    args = p.parse_args(argv)

    # Build job_id → (app, goal_path) map
    job_meta: dict[str, tuple[str, str]] = {}
    if args.jobs_file and args.jobs_file.exists():
        for line in args.jobs_file.read_text().splitlines():
            line = line.strip()
            if not line: continue
            j = json.loads(line)
            jid = j["job_id"]
            job_meta[jid] = (_route(jid, j["goal_path"], j.get("app")), j["goal_path"])

    # Fall back / merge from summary.json
    sj = args.run_dir / "summary.json"
    if sj.exists():
        s = json.loads(sj.read_text())
        for r in s.get("results", []):
            jid = r["job_id"]
            if jid in job_meta: continue
            job_meta[jid] = (_route(jid, r["goal_path"], r.get("app")), r["goal_path"])

    # Discover trajectories
    todo: list[tuple[str, str, str]] = []
    for traj in args.run_dir.glob("worker_*/jobs/*/trajectory.json"):
        jid = traj.parent.name
        if jid not in job_meta:
            print(f"[skip] {jid}: no job_meta")
            continue
        app, gp = job_meta[jid]
        todo.append((str(traj), app, gp))
    print(f"to evaluate: {len(todo)} trajectories with {args.workers} workers")

    n_ok = n_skip = n_err = 0
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(_eval_one, t, a, g, args.force) for (t, a, g) in todo]
        for f in as_completed(futs):
            r = f.result()
            results.append(r)
            status = r.get("status")
            if status == "ok":      n_ok   += 1
            elif status == "skipped": n_skip += 1
            else:                    n_err  += 1
            print(f"[{status:>5}] {r['job']:>55}  match_score={r.get('match_score')}")

    out = args.run_dir / "eval_results.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nDONE ok={n_ok} skip={n_skip} err={n_err} total={len(todo)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
