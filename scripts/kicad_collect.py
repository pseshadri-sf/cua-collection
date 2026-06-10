"""M6 batch collector: run full GUI trajectories on staged KiCad boards.

For each board in the manifest (up to --limit): render the goal atlas, run the
full compositional GUI trajectory (kicad_agent_trajectory.py — planner ->
compositional console replay under Xvfb -> trajectory.mp4 + clean video), then
score it with evaluate_kicad_run. Prints per-board + aggregate match_score and
points at the produced video artifacts.

Sequential (each run owns the Xvfb display). Use the orchestrator
(parallel_orchestrator.py --app kicad) for parallel collection at larger scale.

Usage:
  uv run python scripts/kicad_collect.py --manifest ~/cua_kicad_smoketest/manifest.jsonl \
      --planner google/gemini-3-flash-preview --limit 5 \
      --out-root ~/cua_kicad_smoketest/runs/m6
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--planner", default="google/gemini-3-flash-preview")
    p.add_argument("--executor", default="qwen/qwen3-vl-30b-a3b-instruct")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--out-root", type=Path,
                   default=Path.home() / "cua_kicad_smoketest" / "runs" / "m6")
    p.add_argument("--timeout", type=int, default=600)
    args = p.parse_args(argv)

    from render_kicad_goal import render_goal
    from cua_smoketest.agent.kicad_eval import evaluate_kicad_run

    boards = []
    for line in args.manifest.read_text().splitlines():
        line = line.strip()
        if line:
            j = json.loads(line)
            if j.get("staged_asset") and Path(j["staged_asset"]).exists():
                boards.append(j)
    boards = boards[: args.limit]
    args.out_root.mkdir(parents=True, exist_ok=True)
    print(f"[collect] {len(boards)} boards via GUI ({args.planner})", flush=True)

    results = []
    for i, j in enumerate(boards):
        board = Path(j["staged_asset"])
        goal = render_goal(board)
        if not goal:
            print(f"[collect] [{i}] render failed {board.name}"); continue
        out_dir = args.out_root / board.stem
        cmd = ["uv", "run", "python", str(_REPO / "scripts" / "kicad_agent_trajectory.py"),
               "--goal", str(goal), "--output-dir", str(out_dir),
               "--model", args.executor, "--planner-model", args.planner,
               "--compositional", "--grounded", "--planner-reasoning", "low",
               "--max-steps", str(args.max_steps), "--postprocess"]
        try:
            subprocess.run(cmd, cwd=str(_REPO), capture_output=True, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            print(f"[collect] [{i}] {board.name} TIMEOUT", flush=True)
        traj = out_dir / "trajectory.json"
        score = None
        if traj.exists():
            try:
                ev = evaluate_kicad_run(traj, board, out_dir / "eval")
                (out_dir / "eval" / "eval.json").write_text(json.dumps(ev, indent=2))
                score = ev["score"]["match_score"]
            except Exception as exc:  # noqa: BLE001
                print(f"[collect] [{i}] eval error: {exc}", flush=True)
        vid = out_dir / "video_clean.mp4"
        rec = {"board": board.name, "repo": j.get("repo"),
               "total_components": j.get("total_components"),
               "match_score": score, "video": str(vid) if vid.exists() else None,
               "trajectory": str(traj) if traj.exists() else None}
        results.append(rec)
        print(f"[collect] [{i+1}/{len(boards)}] {board.name:<40} score={score} "
              f"video={'yes' if vid.exists() else 'no'}", flush=True)

    (args.out_root / "collect_summary.json").write_text(json.dumps(results, indent=2))
    scored = [r["match_score"] for r in results if r["match_score"] is not None]
    print("\n==== M6 COLLECTION SUMMARY ====")
    print(f"boards: {len(results)} | with video: {sum(1 for r in results if r['video'])} | "
          f"mean match_score: {sum(scored)/len(scored):.1f}" if scored else "no scores")
    print(f"summary: {args.out_root / 'collect_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
