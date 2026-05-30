"""Decomposition validation driver.

For each asset in the decomposed/ slice:
  1. Build a per-part jobs.jsonl (one job per sub-part screenshot).
  2. Run the existing agent pipeline on each sub-part.
  3. Eval each sub-part trajectory.
  4. Composite: weighted (by bbox-volume) mean of per-part match_scores.
  5. Compare composite to the whole-asset baseline score (from sweep120-v2).

This is a *validation* script: it does NOT change the production pipeline.
It just exercises the existing single-goal runner against decomposed
sub-parts and aggregates results offline.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cua_smoketest.agent.evaluator import (  # noqa: E402
    evaluate_freecad_run, evaluate_blender_run,
    resolve_freecad_goal_asset, resolve_blender_goal_asset,
)


FC_DECOMPOSED = Path.home() / "cua_gui_smoketest" / "decomposed"
BL_DECOMPOSED = Path.home() / "cua_blender_smoketest" / "decomposed"
FC_SHOTS = Path.home() / "cua_gui_smoketest" / "screenshots"
BL_SHOTS = Path.home() / "cua_blender_smoketest" / "screenshots"


def _enumerate(app: str) -> Iterable[tuple[str, dict, Path]]:
    """Yield (asset_stem, manifest, decomposed_dir) for each decomposed asset."""
    root = FC_DECOMPOSED if app == "freecad" else BL_DECOMPOSED
    if not root.exists(): return
    for d in sorted(root.iterdir()):
        m = d / "manifest.json"
        if not m.exists(): continue
        yield (d.name, json.loads(m.read_text()), d)


def _find_sub_goal_png(app: str, asset_stem: str, part_idx: int, prefix: str) -> Path | None:
    """Find the screenshot for SUB__<stem>__part_NN.png. Case-insensitive."""
    dir_ = FC_SHOTS if app == "freecad" else BL_SHOTS
    needle = f"sub__{asset_stem.lower()}__part_{part_idx:02d}"
    for p in dir_.glob(f"{prefix}*_loaded_*.png"):
        if needle in p.name.lower():
            return p
    return None


def build_jobs(app: str, prefix: str, out_path: Path,
               model: str = "qwen/qwen3-vl-30b-a3b-instruct",
               max_steps: int = 20) -> int:
    jobs = []
    for stem, manifest, dec_dir in _enumerate(app):
        for part in manifest["parts"]:
            png = _find_sub_goal_png(app, stem, part["index"], prefix)
            if png is None: continue
            jobs.append({
                "job_id": f"sub_{app[:2]}_{stem}_{part['index']:02d}",
                "app": app,
                "goal_path": str(png),
                "max_steps": max_steps,
                "extra_args": ["--model", model,
                               "--reasoning-effort", "low",
                               "--image-max-dim", "1024"],
                "timeout_sec": 600,
            })
    with open(out_path, "w") as fh:
        for j in jobs:
            fh.write(json.dumps(j) + "\n")
    return len(jobs)


def eval_and_composite(run_dir: Path, jobs_file: Path,
                       baseline_scores: dict | None = None) -> dict:
    """Eval each sub-part trajectory, then composite per asset_stem."""
    jobs = [json.loads(L) for L in open(jobs_file)]
    by_job = {j["job_id"]: j for j in jobs}

    results = []
    for traj_p in sorted(run_dir.glob("worker_*/jobs/sub_*/trajectory.json")):
        jid = traj_p.parent.name
        if jid not in by_job: continue
        j = by_job[jid]
        app = j["app"]
        resolver = resolve_freecad_goal_asset if app == "freecad" else resolve_blender_goal_asset
        runner = evaluate_freecad_run if app == "freecad" else evaluate_blender_run
        asset = resolver(Path(j["goal_path"]))
        score = None
        if asset:
            try:
                edir = traj_p.parent / "eval"
                r = runner(traj_p, asset, edir)
                (edir / "eval.json").write_text(json.dumps(r, indent=2))
                score = r["score"]["match_score"]
            except Exception as e:
                pass
        # jid format: sub_<fr|bl>_<stem>_<NN>
        parts = jid.split("_")
        stem = "_".join(parts[2:-1])
        part_idx = int(parts[-1])
        results.append({"app": app, "stem": stem, "part_idx": part_idx, "score": score})

    # Composite per stem
    composites = []
    by_stem: dict[tuple[str, str], list] = {}
    for r in results:
        by_stem.setdefault((r["app"], r["stem"]), []).append(r)
    for (app, stem), parts in by_stem.items():
        scored = [p for p in parts if p["score"] is not None]
        if not scored: continue
        mean = sum(p["score"] for p in scored) / len(scored)
        # Pull manifest for bbox-volume weighting
        manifest = json.loads(((FC_DECOMPOSED if app=="freecad" else BL_DECOMPOSED) / stem / "manifest.json").read_text())
        weights = {p["index"]: max(1.0, p.get("volume") or p.get("vertex_count") or 1) for p in manifest["parts"]}
        weighted = (
            sum(p["score"] * weights.get(p["part_idx"], 1) for p in scored)
            / sum(weights.get(p["part_idx"], 1) for p in scored)
        )
        baseline = (baseline_scores or {}).get(stem)
        composites.append({
            "app": app, "stem": stem, "n_parts": len(parts), "n_scored": len(scored),
            "mean_per_part_score": round(mean, 1),
            "weighted_composite": round(weighted, 1),
            "baseline_whole_asset_score": baseline,
            "delta_vs_baseline": (round(weighted - baseline, 1) if baseline is not None else None),
            "per_part_scores": {p["part_idx"]: p["score"] for p in scored},
        })
    return {"results": results, "composites": composites}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("subcommand", choices=("build-jobs", "eval"))
    p.add_argument("--app", choices=("freecad", "blender"), required=True)
    p.add_argument("--prefix", required=True, help="Screenshot prefix (F_ for FC, G_ for BL)")
    p.add_argument("--jobs-file", type=Path, required=True)
    p.add_argument("--run-dir", type=Path)
    p.add_argument("--baseline-scores", type=Path, help="JSON {stem: score} for comparison")
    args = p.parse_args()

    if args.subcommand == "build-jobs":
        n = build_jobs(args.app, args.prefix, args.jobs_file)
        print(f"wrote {n} jobs → {args.jobs_file}")
    elif args.subcommand == "eval":
        if not args.run_dir:
            print("ERROR: --run-dir required for eval", file=sys.stderr); return 2
        baseline = json.loads(args.baseline_scores.read_text()) if args.baseline_scores else None
        out = eval_and_composite(args.run_dir, args.jobs_file, baseline)
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
