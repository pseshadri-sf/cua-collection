"""Report performance + speed + cost for a KiCad orchestrator run.

Walks an orchestrator run dir (worker_*/jobs/<job_id>/), and for each job:
  - reconstructs + scores the trajectory (evaluate_kicad_run) -> match_score
  - reads build_plan.json _planner_usage -> prompt/completion/image tokens

Prints: performance (mean/median match_score, distribution), speed (wall time,
per-board, throughput), and cost (planner tokens x flash rates).

Usage:
  uv run python scripts/kicad_run_report.py --run-dir ~/cua_kicad_smoketest/runs/full100 \
      --start-file /tmp/kicad_full100_start.txt --workers 4
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from statistics import mean, median

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

# google/gemini-3-flash-preview pricing ($/token), from OpenRouter.
PRICE_PROMPT = 0.0000005
PRICE_COMPLETION = 0.000003
PRICE_IMAGE = 0.0000005  # per image


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--start-file", type=Path, default=None)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--asset-dir", type=Path,
                   default=Path.home() / "cua_kicad_smoketest" / "assets")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv)

    from cua_smoketest.agent.kicad_eval import evaluate_kicad_run
    from cua_smoketest.agent.evaluator import resolve_kicad_goal_asset

    jobs = sorted(args.run_dir.glob("worker_*/jobs/*/trajectory.json"))
    print(f"[report] {len(jobs)} jobs in {args.run_dir}", flush=True)

    rows = []
    tok_p = tok_c = n_img = 0
    for i, traj in enumerate(jobs):
        jd = traj.parent
        # resolve goal board: prefer the job's goal.png sidecar, else manifest stem
        asset = resolve_kicad_goal_asset(jd / "goal.png")
        if asset is None:
            # fall back: stem from job dir name (ki_NNN_<stem>)
            stem = "_".join(jd.name.split("_")[2:])
            cand = args.asset_dir / f"{stem}.kicad_pcb"
            asset = cand if cand.exists() else None
        score = None; gfp = afp = None
        if asset and asset.exists():
            try:
                ev = evaluate_kicad_run(traj, asset, jd / "eval")
                s = ev["score"]; score = s["match_score"]
                gfp, afp = s["goal_footprints"], s["agent_footprints"]
            except Exception as exc:  # noqa: BLE001
                print(f"  eval err {jd.name}: {exc}", flush=True)
        # planner cost from build_plan.json
        bp = jd / "build_plan.json"
        if bp.exists():
            try:
                u = (json.loads(bp.read_text()).get("_planner_usage") or {})
                tok_p += u.get("prompt_tokens", 0) or 0
                tok_c += u.get("completion_tokens", 0) or 0
                n_img += 1  # one goal atlas per plan
            except Exception:  # noqa: BLE001
                pass
        try:
            tj = json.loads(traj.read_text())
            term = tj.get("terminated_by")
        except Exception:  # noqa: BLE001
            term = None
        rows.append({"job": jd.name, "match_score": score, "goal_fp": gfp,
                     "agent_fp": afp, "terminated_by": term,
                     "video": (jd / "video_clean.mp4").exists()})
        if (i + 1) % 20 == 0:
            print(f"  scored {i+1}/{len(jobs)}", flush=True)

    scored = [r["match_score"] for r in rows if r["match_score"] is not None]
    placed = [(r["agent_fp"], r["goal_fp"]) for r in rows
              if r.get("agent_fp") is not None and r.get("goal_fp")]
    elapsed = None
    if args.start_file and args.start_file.exists():
        elapsed = time.time() - float(args.start_file.read_text().strip())

    cost = tok_p * PRICE_PROMPT + tok_c * PRICE_COMPLETION + n_img * PRICE_IMAGE

    print("\n==================== KiCad 100-board run report ====================")
    print(f"jobs: {len(rows)}  | with clean video: {sum(1 for r in rows if r['video'])}"
          f"  | scored: {len(scored)}")
    print("\n--- PERFORMANCE (match_score 0-100) ---")
    if scored:
        sc = sorted(scored)
        print(f"  mean {mean(scored):.1f} | median {median(scored):.1f} | "
              f"min {sc[0]:.1f} | max {sc[-1]:.1f}")
        for lo, hi in [(0, 10), (10, 30), (30, 50), (50, 70), (70, 101)]:
            c = sum(1 for s in scored if lo <= s < hi)
            print(f"    [{lo:>3},{hi:>3}): {'#'*c} {c}")
    if placed:
        avail = [a / g for a, g in placed if g]
        print(f"  footprint placement: mean agent_fp/goal_fp = {mean(avail):.2f}")
    if elapsed:
        print("\n--- SPEED ---")
        print(f"  wall time: {elapsed/60:.1f} min ({elapsed:.0f}s) | workers: {args.workers}")
        print(f"  per board (wall/n): {elapsed/max(1,len(rows)):.1f}s | "
              f"throughput: {len(rows)/(elapsed/60):.2f} boards/min")
        print(f"  worker-seconds/board: {elapsed*args.workers/max(1,len(rows)):.0f}s")
    print("\n--- COST (planner = google/gemini-3-flash-preview) ---")
    print(f"  prompt tokens: {tok_p:,} (${tok_p*PRICE_PROMPT:.3f}) | "
          f"completion tokens: {tok_c:,} (${tok_c*PRICE_COMPLETION:.3f}) | "
          f"images: {n_img} (${n_img*PRICE_IMAGE:.4f})")
    print(f"  TOTAL planner cost: ${cost:.3f}  | per board: ${cost/max(1,len(rows)):.4f}")
    print("  (compositional mode -> executor VLM not called; planner is the only API cost)")

    out = args.out or (args.run_dir / "run_report.json")
    out.write_text(json.dumps({
        "n_jobs": len(rows), "scored": len(scored),
        "mean_match_score": mean(scored) if scored else None,
        "median_match_score": median(scored) if scored else None,
        "wall_time_s": elapsed, "workers": args.workers,
        "cost_usd": cost, "cost_per_board": cost / max(1, len(rows)),
        "prompt_tokens": tok_p, "completion_tokens": tok_c, "images": n_img,
        "rows": rows,
    }, indent=2))
    print(f"\n[report] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
