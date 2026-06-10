"""Procure KiCad goal boards: scan .kicad_pcb files, difficulty-filter, stage.

Mirrors generate_{freecad,blender}_assets.py. Sources real, editable boards
(the kicad-happy-testharness index of ~5,800 open-source KiCad projects, or any
directory of .kicad_pcb files), scans each for difficulty signals, keeps the
medium-to-difficult band, copies the winners into the smoketest assets dir, and
writes a manifest. Optionally renders the goal atlas per board.

Difficulty is scored by a fast s-expression text scan (no pcbnew load): footprint
count, pad count, net count, track/via count, copper-layer count, board-outline
area. The dominant axis is footprint_count (SPEC §5 methodology).

Usage:
  # from an existing checkout of boards:
  uv run python scripts/procure_kicad_assets.py --source-dir <dir> --limit 50 [--render]
  # clone the kicad-happy index first (shallow), then scan its repos:
  uv run python scripts/procure_kicad_assets.py --clone --limit 50

Output: ~/cua_kicad_smoketest/assets/<stem>.kicad_pcb + manifest.jsonl
        (+ screenshots/<A_n_loaded_stem>.png goal atlases when --render).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_OUT_ROOT = Path.home() / "cua_kicad_smoketest"
_KICAD_HAPPY_REPO = "https://github.com/aklofas/kicad-happy-testharness"

# Counters via cheap regex over the board text (robust across format versions).
_RE_FOOTPRINT = re.compile(r"\(footprint\s")
_RE_PAD = re.compile(r"\(pad\s")
_RE_NET = re.compile(r"\(net\s+\d+\s")        # net declarations
_RE_SEGMENT = re.compile(r"\(segment\s|\(gr_line\s")
_RE_VIA = re.compile(r"\(via\s")
_RE_LAYER_BLOCK = re.compile(r"\(layers\b(.*?)\n\s*\)", re.DOTALL)
_RE_CU_LAYER = re.compile(r'"[A-Za-z0-9_.]*\.Cu"')


def scan_board(path: Path) -> dict | None:
    try:
        txt = path.read_text(errors="ignore")
    except OSError:
        return None
    if "(kicad_pcb" not in txt[:200]:
        return None
    fp = len(_RE_FOOTPRINT.findall(txt))
    pads = len(_RE_PAD.findall(txt))
    nets = len(_RE_NET.findall(txt))
    tracks = len(_RE_SEGMENT.findall(txt))
    vias = len(_RE_VIA.findall(txt))
    m = _RE_LAYER_BLOCK.search(txt)
    cu_layers = len(set(_RE_CU_LAYER.findall(m.group(1)))) if m else 0
    return {"path": str(path), "stem": path.stem,
            "footprint_count": fp, "pad_count": pads, "net_count": nets,
            "track_count": tracks, "via_count": vias, "layer_count": cu_layers}


def difficulty_bucket(s: dict, min_fp: int, max_fp: int) -> str:
    """Bucket by footprint count (dominant axis), gated to a sane band."""
    fp = s["footprint_count"]
    if fp < min_fp:
        return "easy"
    if fp > max_fp:
        return "too_complex"
    if fp < min_fp + (max_fp - min_fp) // 3:
        return "medium"
    return "hard"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--source-dir", type=Path, help="Directory of .kicad_pcb files to scan")
    src.add_argument("--clone", action="store_true",
                     help=f"Shallow-clone {_KICAD_HAPPY_REPO} and scan it")
    p.add_argument("--out-root", type=Path, default=_OUT_ROOT)
    p.add_argument("--limit", type=int, default=50, help="Max boards to stage")
    p.add_argument("--min-footprints", type=int, default=8)
    p.add_argument("--max-footprints", type=int, default=200)
    p.add_argument("--bucket", choices=("medium", "hard", "medium-hard"),
                   default="medium-hard", help="Difficulty band to keep")
    p.add_argument("--render", action="store_true",
                   help="Render the goal atlas per staged board (render_kicad_goal.py)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if args.clone:
        clone_dir = args.out_root / "kicad-happy"
        if not clone_dir.exists():
            print(f"[procure] shallow-cloning {_KICAD_HAPPY_REPO} -> {clone_dir}")
            clone_dir.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["git", "clone", "--depth", "1", _KICAD_HAPPY_REPO,
                               str(clone_dir)], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"[procure] clone failed: {r.stderr[:300]}", file=sys.stderr)
                print("[procure] NOTE: the index repo references upstream projects via "
                      "checkout.py; run that to fetch the actual boards, then re-run with "
                      "--source-dir.", file=sys.stderr)
                return 1
        source_dir = clone_dir
    else:
        source_dir = args.source_dir
        if not source_dir.exists():
            print(f"[procure] source-dir not found: {source_dir}", file=sys.stderr)
            return 2

    boards = sorted(source_dir.rglob("*.kicad_pcb"))
    print(f"[procure] scanning {len(boards)} .kicad_pcb files under {source_dir}")
    keep = {"medium", "hard"} if args.bucket == "medium-hard" else {args.bucket}
    scanned = []
    for b in boards:
        s = scan_board(b)
        if not s:
            continue
        s["bucket"] = difficulty_bucket(s, args.min_footprints, args.max_footprints)
        if s["bucket"] in keep:
            scanned.append(s)
    # Order by footprint count so a --limit sample spans the band.
    scanned.sort(key=lambda s: s["footprint_count"])
    selected = scanned[:: max(1, len(scanned) // args.limit)][:args.limit] if scanned else []
    print(f"[procure] {len(scanned)} in band ({args.bucket}); selecting {len(selected)}")

    if args.dry_run:
        for s in selected:
            print(f"  [{s['bucket']:>6}] fp={s['footprint_count']:>3} nets={s['net_count']:>3} "
                  f"layers={s['layer_count']} {s['stem']}")
        return 0

    assets_dir = args.out_root / "assets"
    shots_dir = args.out_root / "screenshots"
    assets_dir.mkdir(parents=True, exist_ok=True)
    shots_dir.mkdir(parents=True, exist_ok=True)
    manifest = args.out_root / "manifest.jsonl"
    n = 0
    with open(manifest, "w") as mf:
        for i, s in enumerate(selected):
            # de-dup stems by prefixing an index where needed
            stem = s["stem"]
            dst = assets_dir / f"{stem}.kicad_pcb"
            if dst.exists():
                stem = f"{stem}_{i:03d}"
                dst = assets_dir / f"{stem}.kicad_pcb"
            shutil.copyfile(s["path"], dst)
            rec = {**s, "staged_asset": str(dst), "stem": stem}
            if args.render:
                goal_png = shots_dir / f"A_{i:03d}_loaded_{stem}.png"
                rr = subprocess.run(
                    ["uv", "run", "python", str(_REPO / "scripts" / "render_kicad_goal.py"),
                     "--asset", str(dst), "--out", str(goal_png)],
                    capture_output=True, text=True, cwd=str(_REPO))
                rec["goal_png"] = str(goal_png) if goal_png.exists() else None
                rec["render_rc"] = rr.returncode
            mf.write(json.dumps(rec) + "\n")
            n += 1
    print(f"[procure] staged {n} boards -> {assets_dir}\n[procure] manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
