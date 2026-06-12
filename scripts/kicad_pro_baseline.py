"""Full pro@low sweep on the 50-board ablation set, decomp default OFF + json
repair in place. This is the production baseline.

Compares to:
  - fix1_results.jsonl       (flash@low + decomp OFF — current production)
  - decomp_results.jsonl     (flash@low + decomp ON  — confirmed inert)
  - modelscan_*pro*d-1.jsonl (pro@low + decomp ON, n=48 — partial baseline)

Outputs:
  - pro_baseline_results.jsonl
  - prints paired comparison vs fix#1 baseline (the production lever check)
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", type=Path,
                   default=Path("/home/ubuntu/cua_kicad_smoketest/manifest.jsonl"))
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--out", type=Path,
                   default=Path("/home/ubuntu/cua_kicad_smoketest/pro_baseline_results.jsonl"))
    p.add_argument("--baseline", type=Path,
                   default=Path("/home/ubuntu/cua_kicad_smoketest/fix1_results.jsonl"))
    args = p.parse_args(argv)

    cmd = [
        "uv", "run", "python", "-u", str(_REPO / "scripts" / "kicad_plan_ablation.py"),
        "--manifest", str(args.manifest),
        "--planners", "google/gemini-3.1-pro-preview",
        "--reasoning", "low",
        "--limit", str(args.limit),
        "--out", str(args.out),
    ]
    print(f"[pro-baseline] launching: {' '.join(cmd)}", flush=True)
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        print(f"[pro-baseline] ablation exit={rc}", flush=True)

    # Paired comparison
    rows = [json.loads(l) for l in args.out.read_text().splitlines() if l.strip()]
    base_rows = [json.loads(l) for l in args.baseline.read_text().splitlines() if l.strip()]
    pro = {r["board"]: r for r in rows if r.get("match_score") is not None}
    base = {r["board"]: r for r in base_rows if r.get("match_score") is not None}
    common = sorted(set(pro) & set(base))

    print(f"\n==== PRO@low vs FLASH@low (PRODUCTION) — paired ====")
    print(f"  pro success: {len(pro)}/50    base success: {len(base)}/50    common: {len(common)}")
    if common:
        ps = [pro[b]["match_score"] for b in common]
        bs = [base[b]["match_score"] for b in common]
        d = [a - b for a, b in zip(ps, bs)]
        up = [x for x in d if x > 1]
        dn = [x for x in d if x < -1]
        print(f"  flash@low mean: {st.mean(bs):.1f}    "
              f"pro@low mean: {st.mean(ps):.1f}    "
              f"Δmean: {st.mean(d):+.2f}    Δmedian: {st.median(d):+.2f}")
        print(f"  boards UP >1pt:   {len(up)}/{len(common)}")
        print(f"  boards DOWN >1pt: {len(dn)}/{len(common)}")
        print(f"\n  TOP UPGRADES:")
        for b in sorted(common, key=lambda x: pro[x]["match_score"] - base[x]["match_score"], reverse=True)[:10]:
            print(f"    {b[:50]:<52} {base[b]['match_score']:.1f} -> {pro[b]['match_score']:.1f}   Δ {pro[b]['match_score']-base[b]['match_score']:+.1f}")
        print(f"\n  TOP REGRESSIONS:")
        for b in sorted(common, key=lambda x: pro[x]["match_score"] - base[x]["match_score"])[:5]:
            d = pro[b]["match_score"] - base[b]["match_score"]
            if d < 0:
                print(f"    {b[:50]:<52} {base[b]['match_score']:.1f} -> {pro[b]['match_score']:.1f}   Δ {d:+.1f}")

    # Parse-failure rate (the JSON repair check)
    parse_fail = sum(1 for r in rows if r.get("match_score") is None)
    print(f"\n  parse failures (post-repair): {parse_fail}/50")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
