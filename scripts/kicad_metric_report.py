"""M4/M5 report: analyze the KiCad planner-ablation output.

Reads ablation.jsonl (rows of {board, planner, match_score, placement, nets,
outline, counts, goal_fp, agent_fp, total_components, tokens_out}) and prints:
  - per-planner aggregate (mean match_score, mean output tokens, win rate)
  - metric component means (placement/nets/outline/counts)
  - the placement ceiling: agent_fp/goal_fp (how many footprints were loadable)
  - correlation of match_score with board complexity + footprint availability
  - degenerate cases worth a metric/tolerance look

Usage: uv run python scripts/kicad_metric_report.py --in ~/cua_kicad_smoketest/ablation.jsonl
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev


def _corr(xs, ys):
    if len(xs) < 3:
        return 0.0
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    return num / (dx * dy) if dx and dy else 0.0


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="inp", type=Path, required=True)
    args = p.parse_args(argv)
    rows = [json.loads(l) for l in args.inp.read_text().splitlines() if l.strip()]
    ok = [r for r in rows if r.get("match_score") is not None]
    print(f"== KiCad ablation report: {len(rows)} rows, {len(ok)} scored ==\n")

    byp = defaultdict(list)
    for r in ok:
        byp[r["planner"]].append(r)
    print("PER-PLANNER")
    print(f"  {'model':<40}{'n':>4}{'mean':>8}{'std':>7}{'tok_out':>9}")
    for m, rs in sorted(byp.items()):
        sc = [r["match_score"] for r in rs]
        tk = [r["tokens_out"] for r in rs if r.get("tokens_out")]
        print(f"  {m.split('/')[-1]:<40}{len(sc):>4}{mean(sc):>8.1f}{pstdev(sc):>7.1f}"
              f"{(mean(tk) if tk else 0):>9.0f}")

    # win rate per board (which planner scored highest)
    byboard = defaultdict(dict)
    for r in ok:
        byboard[r["board"]][r["planner"]] = r["match_score"]
    wins = defaultdict(int); ties = 0
    for b, d in byboard.items():
        top = max(d.values())
        winners = [m for m, v in d.items() if v >= top - 0.05]
        if len(winners) == len(d):
            ties += 1
        for m in winners:
            wins[m] += 1
    print(f"\nWIN RATE (highest score per board; {len(byboard)} boards, {ties} all-tie)")
    for m in sorted(byp):
        print(f"  {m.split('/')[-1]:<40}{wins[m]:>3}/{len(byboard)}")

    print("\nMETRIC COMPONENTS (mean over all scored)")
    for comp in ("placement", "nets", "outline", "counts"):
        vals = [r[comp] for r in ok if r.get(comp) is not None]
        if vals:
            print(f"  {comp:<12}{mean(vals):>6.3f}")

    print("\nPLACEMENT CEILING (footprint availability)")
    avail = [(r["agent_fp"] / r["goal_fp"]) for r in ok
             if r.get("goal_fp") and r.get("agent_fp") is not None]
    if avail:
        print(f"  mean agent_fp/goal_fp = {mean(avail):.2f}  (1.0 = all footprints loadable)")
        low = sum(1 for a in avail if a < 0.5)
        print(f"  boards where <50% of footprints were loadable: {low}/{len(avail)} "
              f"(custom/project libs not in the system install)")

    print("\nCORRELATIONS (match_score vs ...)")
    comp_x = [r["total_components"] for r in ok if r.get("total_components")]
    comp_y = [r["match_score"] for r in ok if r.get("total_components")]
    print(f"  total_components : r={_corr(comp_x, comp_y):+.2f}")
    av_x = [r["agent_fp"] / r["goal_fp"] for r in ok if r.get("goal_fp") and r.get("agent_fp") is not None]
    av_y = [r["match_score"] for r in ok if r.get("goal_fp") and r.get("agent_fp") is not None]
    print(f"  footprint_avail  : r={_corr(av_x, av_y):+.2f}  (expect high — availability caps the score)")

    print("\nDEGENERATE / LOW cases (match_score < 15)")
    for r in sorted(ok, key=lambda r: r["match_score"])[:6]:
        print(f"  {r['match_score']:>5.1f}  {r['board']:<34} "
              f"fp {r.get('agent_fp')}/{r.get('goal_fp')} place={r.get('placement')} "
              f"nets={r.get('nets')} [{r['planner'].split('/')[-1]}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
