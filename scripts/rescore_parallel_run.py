"""Re-score every job in a parallel-orchestrator output directory using the
*current* evaluator weights, without re-replaying agent code.

Walks `<run>/worker_NNN/jobs/JOB_ID/eval/eval.json`, reconstructs
`GeometryStats` from the recorded `agent_stats` / `goal_stats` blobs, calls
`compare()` (which uses the live weights), and writes:

  <run>/rescored_summary.csv
  <run>/rescored_summary.json

Use this to A/B old vs new reward weights against a frozen set of trajectories.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from dataclasses import asdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cua_smoketest.agent.evaluator import (   # noqa: E402
    GeometryStats, compare,
)


def _stats_from_dict(d: dict) -> GeometryStats:
    return GeometryStats(
        found=d.get("found", False),
        object_count=d.get("object_count", 0),
        object_names=d.get("object_names") or [],
        volume=float(d.get("volume", 0.0)),
        surface_area=float(d.get("surface_area", 0.0)),
        bbox=tuple(d.get("bbox") or (0.0, 0.0, 0.0)),  # type: ignore[arg-type]
        face_count=d.get("face_count", 0),
        edge_count=d.get("edge_count", 0),
        vertex_count=d.get("vertex_count", 0),
        error=d.get("error"),
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run_dir", type=Path)
    p.add_argument("--label", default="rescored",
                   help="Output basename: <run>/<label>_summary.{csv,json}")
    args = p.parse_args(argv)

    rows: list[dict] = []
    for worker_dir in sorted(p for p in args.run_dir.iterdir()
                             if p.is_dir() and p.name.startswith("worker_")):
        jobs_dir = worker_dir / "jobs"
        if not jobs_dir.is_dir(): continue
        for job_dir in sorted(jobs_dir.iterdir()):
            ej = job_dir / "eval" / "eval.json"
            if not ej.exists(): continue
            try:
                e = json.loads(ej.read_text())
            except json.JSONDecodeError:
                continue
            goal  = _stats_from_dict(e.get("goal_stats")  or {})
            agent = _stats_from_dict(e.get("agent_stats") or {})
            score = compare(goal, agent)
            row = {
                "worker": worker_dir.name,
                "job":    job_dir.name,
                "app":    e.get("app", "?"),
                **{f"score_{k}": v for k, v in asdict(score).items()},
            }
            rows.append(row)

    if not rows:
        print("no eval.json found", file=sys.stderr); return 1

    # Dedupe by job_id keeping the best match_score across worker dirs / retries
    best_by_job: dict[str, dict] = {}
    for r in rows:
        key = r["job"]
        if key not in best_by_job or r["score_match_score"] > best_by_job[key]["score_match_score"]:
            best_by_job[key] = r
    rows = list(best_by_job.values())

    out_csv  = args.run_dir / f"{args.label}_summary.csv"
    out_json = args.run_dir / f"{args.label}_summary.json"
    with out_csv.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

    by_app: dict[str, list[float]] = {}
    for r in rows:
        by_app.setdefault(r["app"], []).append(r["score_match_score"])
    summary = {
        "n_jobs":   len(rows),
        "by_app":   {a: {"n": len(v),
                         "mean": round(statistics.mean(v), 2),
                         "median": round(statistics.median(v), 2),
                         "max": max(v), "min": min(v)}
                     for a, v in by_app.items()},
        "components_mean": {
            k: round(statistics.mean([r[f"score_{k}"] for r in rows]), 3)
            for k in ("obj_present", "name_overlap", "vol_ratio", "bbox_score",
                      "shape_proportions", "face_ratio", "vert_ratio",
                      "match_score")
            if f"score_{k}" in rows[0]
        },
    }
    out_json.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {out_csv} ({len(rows)} rows)")
    print(f"wrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
