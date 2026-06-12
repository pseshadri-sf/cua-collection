"""Run kicad_plan_ablation across (model, reasoning, decomp on/off) cells.

Existing ablation script runs one (model, reasoning) per invocation; decomp
is toggled via KICAD_DECOMP_ENABLED env. We orchestrate the cells here, write
per-cell jsonl, then print a combined pivot.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

CELLS = [
    # (label,                        model,                            reasoning, decomp)
    ("flash@medium  decomp=ON",    "google/gemini-3-flash-preview",   "medium",  "1"),
    ("flash@medium  decomp=OFF",   "google/gemini-3-flash-preview",   "medium",  "0"),
    ("pro@low       decomp=ON",    "google/gemini-3.1-pro-preview",   "low",     "1"),
    ("pro@low       decomp=OFF",   "google/gemini-3.1-pro-preview",   "low",     "0"),
]


def run_cell(label, model, reasoning, decomp_flag, manifest, limit, out_dir):
    safe = (model.replace("/", "_") + f"_r-{reasoning}_d-{decomp_flag}")
    out_path = out_dir / f"modelscan_{safe}.jsonl"
    env = os.environ.copy()
    env["KICAD_DECOMP_ENABLED"] = decomp_flag
    print(f"\n========================================================")
    print(f"[cell] {label}   -> {out_path.name}")
    print(f"========================================================", flush=True)
    cmd = [
        "uv", "run", "python", str(_REPO / "scripts" / "kicad_plan_ablation.py"),
        "--manifest", str(manifest),
        "--planners", model,
        "--reasoning", reasoning,
        "--limit", str(limit),
        "--out", str(out_path),
    ]
    subprocess.run(cmd, env=env, check=False)
    return out_path


def summarize(label, jsonl_path):
    if not jsonl_path.exists():
        return None
    rows = [json.loads(l) for l in jsonl_path.read_text().splitlines() if l.strip()]
    scores = [r["match_score"] for r in rows if r.get("match_score") is not None]
    toks = [r["tokens_out"] for r in rows if r.get("tokens_out")]
    return {
        "label": label, "jsonl": jsonl_path.name, "n": len(scores),
        "mean": st.mean(scores) if scores else 0.0,
        "median": st.median(scores) if scores else 0.0,
        "stdev": st.pstdev(scores) if len(scores) > 1 else 0.0,
        "mean_tokens_out": st.mean(toks) if toks else 0.0,
        "rows": {r["board"]: r for r in rows if r.get("match_score") is not None},
    }


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", type=Path,
                   default=Path("/home/ubuntu/cua_kicad_smoketest/manifest.jsonl"))
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--out-dir", type=Path,
                   default=Path("/home/ubuntu/cua_kicad_smoketest"))
    p.add_argument("--cells",
                   help="comma-separated indices into CELLS, default all")
    args = p.parse_args(argv)

    cells = list(range(len(CELLS)))
    if args.cells:
        cells = [int(c) for c in args.cells.split(",") if c.strip()]

    paths = {}
    for ci in cells:
        label, model, reasoning, decomp = CELLS[ci]
        paths[label] = run_cell(label, model, reasoning, decomp,
                                args.manifest, args.limit, args.out_dir)

    print("\n\n==== MODELSCAN SUMMARY ====")
    print(f"{'cell':<30}{'n':>4}{'mean':>8}{'median':>8}{'stdev':>8}{'tok_out':>10}")
    sums = {}
    for label, jp in paths.items():
        s = summarize(label, jp)
        if not s:
            continue
        sums[label] = s
        print(f"{label:<30}{s['n']:>4}{s['mean']:>8.1f}"
              f"{s['median']:>8.1f}{s['stdev']:>8.1f}{s['mean_tokens_out']:>10.0f}")

    # paired comparisons (decomp on - decomp off) per model
    pairs = [
        ("flash@medium  decomp=ON", "flash@medium  decomp=OFF"),
        ("pro@low       decomp=ON", "pro@low       decomp=OFF"),
    ]
    print("\n==== DECOMP IMPACT (paired, decomp_ON - decomp_OFF) ====")
    for on_lbl, off_lbl in pairs:
        on, off = sums.get(on_lbl), sums.get(off_lbl)
        if not (on and off):
            continue
        common = set(on["rows"]) & set(off["rows"])
        if not common:
            continue
        deltas = [on["rows"][b]["match_score"] - off["rows"][b]["match_score"]
                  for b in common]
        moved = sum(1 for d in deltas if abs(d) > 1)
        print(f"  {on_lbl[:14]:<16}  n={len(deltas)}  "
              f"Δmean={st.mean(deltas):+.2f}  Δmedian={st.median(deltas):+.2f}  "
              f"moved>1pt={moved}/{len(deltas)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
