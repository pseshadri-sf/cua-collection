"""Re-evaluate the broken (score=0) KiCad boards with their source-repo libs
and shared third-party libs registered. Isolates the question: how much of the
broken-cluster damage is fixable purely by sourcing libraries?

For each broken board we:
  - resolve the cloned source repo at <project_libs_root>/<repo-slug>
  - resolve any shared third-party .pretty dirs scanned from <extra_libs_root>
  - set KIRECON_PROJECT_LIBS_ROOT (single board's repo dir) + KICAD_EXTRA_LIB_ROOTS
    (colon-separated extra roots, applies to every board)
  - call the planner (default flash@low, optionally also pro@low) and score

Outputs JSONL with the rescue scores so the user can compare to flash_baseline
+ pro_baseline (saved earlier) and see lift-from-libraries cleanly.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

from cua_smoketest.agent.frontier_planner import FrontierPlanner  # noqa: E402
from cua_smoketest.agent.vlm_client import _extract_goal_name      # noqa: E402
from cua_smoketest.agent.kicad_eval import evaluate_kicad_run      # noqa: E402
from render_kicad_goal import render_goal                          # noqa: E402


def _slug(repo: str) -> str:
    return repo.replace("/", "__")


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _scan_extra_roots(extra_libs_root: Path) -> list[str]:
    """Walk extra-libs cache, return parent dirs of every .pretty subdir."""
    if not extra_libs_root.exists():
        return []
    roots = set()
    for p in extra_libs_root.rglob("*.pretty"):
        if p.is_dir():
            roots.add(str(p.parent))
    return sorted(roots)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path,
                    default=Path("/home/ubuntu/cua_kicad_smoketest/manifest.jsonl"))
    ap.add_argument("--boards-list", type=Path, default=None,
                    help="JSON list of board basenames to rescue (default: derive "
                         "from --baseline by selecting score=0)")
    ap.add_argument("--baseline", type=Path,
                    default=Path("/home/ubuntu/cua_kicad_smoketest/flashlow_baseline.jsonl"))
    ap.add_argument("--project-libs-root", type=Path,
                    default=Path("/home/ubuntu/kicad-project-libs"))
    ap.add_argument("--extra-libs-root", type=Path,
                    default=Path("/home/ubuntu/kicad-extra-libs"))
    ap.add_argument("--no-project-libs", action="store_true",
                    help="Run with NO project-libs and NO extra-libs roots set "
                         "— a stripped baseline measuring just the shim fix.")
    ap.add_argument("--planners", default="google/gemini-3-flash-preview,google/gemini-3.1-pro-preview",
                    help="comma-separated planner model ids (each board planned by each)")
    ap.add_argument("--reasoning", default="low")
    ap.add_argument("--out", type=Path,
                    default=Path("/home/ubuntu/cua_kicad_smoketest/lib_rescue_results.jsonl"))
    ap.add_argument("--env-file", type=Path, default=Path("/home/ubuntu/.env"))
    ap.add_argument("--api-key-var", default="OPENAI_API_KEY")
    args = ap.parse_args(argv)

    _load_env(args.env_file)
    api_key = os.environ.get(args.api_key_var, "")
    if not api_key:
        print(f"ERROR: {args.api_key_var} not set", file=sys.stderr); return 2

    # Decide which boards to rescue
    if args.boards_list and args.boards_list.exists():
        targets = set(json.loads(args.boards_list.read_text()))
    else:
        baseline = [json.loads(l) for l in args.baseline.read_text().splitlines() if l.strip()]
        targets = {r["board"] for r in baseline
                   if r.get("match_score") is not None and r["match_score"] < 1.0}
    print(f"[rescue] targeting {len(targets)} boards")

    # Shared extra-libs roots
    extra_roots = _scan_extra_roots(args.extra_libs_root)
    print(f"[rescue] extra .pretty roots (KICAD_EXTRA_LIB_ROOTS):")
    for r in extra_roots:
        print(f"          {r}")
    extra_env = ":".join(extra_roots)

    # Manifest by board name
    manifest = {Path(m["staged_asset"]).name: m for m in
                [json.loads(l) for l in args.manifest.read_text().splitlines() if l.strip()]
                if m.get("staged_asset")}

    planners = [m.strip() for m in args.planners.split(",") if m.strip()]
    out_f = open(args.out, "w")
    n_done = 0
    for b in sorted(targets):
        if b not in manifest:
            print(f"[rescue] skip {b}: not in manifest"); continue
        m = manifest[b]
        board = Path(m["staged_asset"])
        repo = m.get("repo") or ""
        project_dir = args.project_libs_root / _slug(repo) if repo else None
        proj_env = str(project_dir) if project_dir and project_dir.exists() else ""

        goal_png = render_goal(board)
        if not goal_png:
            print(f"[rescue] skip {b}: render failed"); continue
        gname = _extract_goal_name(goal_png)

        # Apply env for this board's reconstruct subprocess
        env_backup = {k: os.environ.get(k) for k in
                      ("KIRECON_PROJECT_LIBS_ROOT", "KICAD_EXTRA_LIB_ROOTS")}
        if args.no_project_libs:
            os.environ.pop("KIRECON_PROJECT_LIBS_ROOT", None)
            os.environ.pop("KICAD_EXTRA_LIB_ROOTS", None)
        else:
            os.environ["KIRECON_PROJECT_LIBS_ROOT"] = proj_env
            os.environ["KICAD_EXTRA_LIB_ROOTS"] = extra_env

        try:
            for model in planners:
                row = {"board": b, "repo": repo, "planner": model,
                       "project_libs_root": proj_env,
                       "extra_libs_count": len(extra_roots),
                       "match_score": None, "tokens_out": None}
                try:
                    planner = FrontierPlanner(api_key=api_key, model=model, app="kicad",
                                              reasoning_effort=args.reasoning,
                                              compositional=False)
                    plan = planner.plan(goal_png, goal_name=gname)
                except Exception as exc:
                    print(f"[rescue] {b[:36]} {model[:35]} plan error: {exc}")
                    out_f.write(json.dumps(row) + "\n"); out_f.flush(); continue
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
                out_f.write(json.dumps(row) + "\n"); out_f.flush()
                print(f"  [{b[:36]:<38}] {model.split('/')[-1][:25]:<26} -> "
                      f"score={row['match_score']}  matched={row.get('matched')}/{row.get('goal_fp')}")
        finally:
            for k, v in env_backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        n_done += 1
        print(f"[rescue] {n_done}/{len(targets)} boards done\n")

    out_f.close()
    # Summary: per-planner rescue, plus aggregate over both planners
    print("\n==== LIB RESCUE SUMMARY ====")
    rows = [json.loads(l) for l in args.out.read_text().splitlines() if l.strip()]
    by_planner = {}
    by_board = {}
    for r in rows:
        by_planner.setdefault(r["planner"], []).append(r)
        b = r["board"]
        by_board.setdefault(b, []).append(r)
    for m, rr in by_planner.items():
        ok = [r for r in rr if r.get("match_score") is not None]
        rescued = [r for r in ok if r["match_score"] > 1.0]
        print(f"  {m:<42}  ok={len(ok)}/{len(rr)}  rescued (>1pt) = {len(rescued)}")
    # per-board best
    best_per = []
    for b, rr in by_board.items():
        ok = [r for r in rr if r.get("match_score") is not None]
        if not ok:
            continue
        best = max(ok, key=lambda r: r["match_score"])
        best_per.append((b, best["match_score"], best["planner"]))
    rescued = [t for t in best_per if t[1] > 1.0]
    print(f"\n  Per-board best (oracle hybrid over planners):")
    print(f"    rescued (>1pt):    {len(rescued)}/{len(best_per)}")
    print(f"    rescued (>=20pt):  {sum(1 for t in rescued if t[1] >= 20)}")
    print(f"    rescued (>=50pt):  {sum(1 for t in rescued if t[1] >= 50)}")
    print(f"\n  Top rescues:")
    for b, s, m in sorted(rescued, key=lambda t: -t[1])[:15]:
        print(f"    {b[:40]:<42}  {s:>5.1f}  ({m.split('/')[-1]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
