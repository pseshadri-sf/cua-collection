"""A/B compare two parallel-orchestrator runs by job_id.

Reads both runs' `<label>_summary.json` (produced by rescore_parallel_run.py),
joins by job_id, prints per-job and per-app deltas.

Usage:
    python compare_waves.py \\
        --baseline /path/to/wave6_summary.json \\
        --treatment /path/to/wave7_summary.json
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def _load_scores(path: Path) -> dict:
    """Returns {job_id: {match_score, app, ...}}."""
    if not path.exists():
        sys.exit(f"missing: {path}")
    d = json.loads(path.read_text())
    rows = d if isinstance(d, list) else d.get("results") or d.get("rows") or []
    out: dict = {}
    for r in rows:
        jid = r.get("job") or r.get("job_id")
        if not jid: continue
        ms = r.get("match_score")
        if ms is None:
            ms = (r.get("score") or {}).get("match_score")
        if ms is None:
            ms = r.get("score_match_score")
        out[jid] = {"match_score": ms, "app": r.get("app", "?")}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", type=Path, required=True,
                    help="Path to baseline run's <label>_summary.json or eval_results.json")
    ap.add_argument("--treatment", type=Path, required=True,
                    help="Path to treatment run's <label>_summary.json or eval_results.json")
    ap.add_argument("--baseline-name", default="baseline")
    ap.add_argument("--treatment-name", default="treatment")
    args = ap.parse_args(argv)

    b = _load_scores(args.baseline)
    t = _load_scores(args.treatment)
    joined = sorted(set(b) & set(t))
    only_b = set(b) - set(t)
    only_t = set(t) - set(b)

    if not joined:
        sys.exit("no shared jobs between runs")

    print(f"# {args.baseline_name} vs {args.treatment_name}")
    print(f"shared:  {len(joined)}")
    print(f"only_{args.baseline_name}: {len(only_b)}")
    print(f"only_{args.treatment_name}: {len(only_t)}")
    print()

    # Per-job deltas, sorted by delta desc
    rows = []
    for jid in joined:
        bs = b[jid]["match_score"] or 0.0
        ts = t[jid]["match_score"] or 0.0
        app = t[jid]["app"] or b[jid]["app"]
        rows.append((jid, app, bs, ts, ts - bs))
    rows.sort(key=lambda r: -r[4])

    print(f"{'JOB':<55} {'APP':<8} {'BASE':>8} {'TREAT':>8} {'Δ':>8}")
    print("-" * 92)
    for jid, app, bs, ts, d in rows:
        sign = "+" if d > 0 else ("-" if d < 0 else " ")
        print(f"{jid:<55} {app:<8} {bs:>8.1f} {ts:>8.1f} {sign}{abs(d):>7.1f}")
    print()

    # Per-app aggregates
    def _agg(group: list[tuple]) -> tuple:
        if not group: return (0, 0.0, 0.0, 0.0, 0, 0, 0)
        n = len(group)
        bm = sum(x[2] for x in group) / n
        tm = sum(x[3] for x in group) / n
        dm = tm - bm
        wins = sum(1 for x in group if x[4] > 0.5)
        losses = sum(1 for x in group if x[4] < -0.5)
        ties = n - wins - losses
        return (n, bm, tm, dm, wins, losses, ties)

    print(f"{'APP':<10} {'N':>4} {'BASE':>7} {'TREAT':>7} {'ΔMEAN':>8} {'WIN':>5} {'LOSS':>5} {'TIE':>5}")
    print("-" * 60)
    by_app: dict = defaultdict(list)
    for r in rows: by_app[r[1]].append(r)
    for app in sorted(by_app):
        n, bm, tm, dm, w, l, ti = _agg(by_app[app])
        print(f"{app:<10} {n:>4} {bm:>7.1f} {tm:>7.1f} {dm:>+8.1f} {w:>5} {l:>5} {ti:>5}")
    n, bm, tm, dm, w, l, ti = _agg(rows)
    print(f"{'OVERALL':<10} {n:>4} {bm:>7.1f} {tm:>7.1f} {dm:>+8.1f} {w:>5} {l:>5} {ti:>5}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
