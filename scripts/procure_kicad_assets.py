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
_SYS_PY = "/usr/bin/python3"


def _catalog_select(catalog: Path, lo: int, hi: int, seed: int) -> list[dict]:
    """Select medium-difficulty repos from the kicad-happy repo_catalog.json:
    >=1 pcb file, total_components in [lo,hi], a modern (KiCad 6+) file version.
    Shuffled (seeded) so a --limit sample spans the band's density."""
    import random
    cat = json.loads(Path(catalog).read_text())

    def modern(r: dict) -> bool:
        best = 0
        for v in (r.get("file_versions") or []):
            m = re.match(r"^(\d{8})$", str(v))
            if m:
                best = max(best, int(m.group(1)))
        return best >= 20200000

    band = [r for r in cat
            if r.get("pcb_files", 0) >= 1
            and lo <= (r.get("complexity") or {}).get("total_components", 0) <= hi
            and modern(r)]
    random.Random(seed).shuffle(band)
    return band


def _valid_board(path: Path, min_fp: int) -> bool:
    """Confirm pcbnew can load the board and it has >= min_fp footprints."""
    try:
        r = subprocess.run(
            [_SYS_PY, "-c", f"import pcbnew; b=pcbnew.LoadBoard({str(path)!r}); "
                            f"assert len(list(b.GetFootprints()))>={min_fp}"],
            capture_output=True, timeout=60)
        return r.returncode == 0
    except subprocess.SubprocessError:
        return False

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


def _procure_from_catalog(args) -> int:
    """Catalog-driven sourcing: select medium repos, clone+extract+validate,
    stage until --limit. Frees each clone after extraction to bound disk."""
    cands = _catalog_select(args.catalog, args.components_lo, args.components_hi, args.seed)
    print(f"[procure] catalog band [{args.components_lo},{args.components_hi}]: "
          f"{len(cands)} modern repos; staging up to {args.limit}")
    args.clones_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = args.out_root / "assets"
    shots_dir = args.out_root / "screenshots"
    assets_dir.mkdir(parents=True, exist_ok=True)
    shots_dir.mkdir(parents=True, exist_ok=True)
    manifest = args.out_root / "manifest.jsonl"
    staged = 0
    with open(manifest, "w") as mf:
        for r in cands:
            if staged >= args.limit:
                break
            dest = args.clones_dir / r["repo"].replace("/", "__")
            if not dest.exists():
                cl = subprocess.run(["git", "clone", "--depth", "1", "--quiet",
                                    r["url"], str(dest)], capture_output=True, timeout=180)
                if cl.returncode != 0:
                    continue
            pcbs = sorted((p for p in dest.rglob("*.kicad_pcb")
                           if p.stat().st_size > 3000),
                          key=lambda p: p.stat().st_size, reverse=True)
            board = pcbs[0] if pcbs else None
            if board is None or not _valid_board(board, args.min_footprints):
                shutil.rmtree(dest, ignore_errors=True); continue
            stem = re.sub(r"[^A-Za-z0-9_-]", "_", r["repo"].split("/")[-1])[:48]
            dst = assets_dir / f"{stem}.kicad_pcb"
            k = 0
            while dst.exists():
                k += 1; dst = assets_dir / f"{stem}_{k}.kicad_pcb"
            shutil.copyfile(board, dst)
            c = r.get("complexity") or {}
            rec = {"staged_asset": str(dst), "repo": r["repo"], "url": r["url"],
                   "src_board": str(board.relative_to(dest)),
                   "total_components": c.get("total_components"), "total_nets": c.get("total_nets"),
                   "pcb_layers_max": c.get("pcb_layers_max"),
                   "board_area_mm2_max": c.get("board_area_mm2_max"), "smd_ratio": c.get("smd_ratio")}
            if args.render:
                goal_png = shots_dir / f"A_{staged:03d}_loaded_{stem}.png"
                rr = subprocess.run(
                    ["uv", "run", "python", str(_REPO / "scripts" / "render_kicad_goal.py"),
                     "--asset", str(dst), "--out", str(goal_png)],
                    capture_output=True, text=True, cwd=str(_REPO))
                rec["goal_png"] = str(goal_png) if goal_png.exists() else None
            mf.write(json.dumps(rec) + "\n"); mf.flush()
            staged += 1
            shutil.rmtree(dest, ignore_errors=True)
            if staged % 10 == 0:
                print(f"[procure] staged {staged}/{args.limit}")
    print(f"[procure] staged {staged} boards -> {assets_dir}\n[procure] manifest: {manifest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--source-dir", type=Path, help="Directory of .kicad_pcb files to scan")
    src.add_argument("--clone", action="store_true",
                     help=f"Shallow-clone {_KICAD_HAPPY_REPO} and scan it")
    src.add_argument("--catalog", type=Path,
                     help="kicad-happy reference/repo_catalog.json — select medium-difficulty "
                          "repos by component count, clone each (pinned), extract+validate one "
                          "board, stage until --limit. The recommended source.")
    p.add_argument("--components-lo", type=int, default=44, help="catalog: min total_components")
    p.add_argument("--components-hi", type=int, default=203, help="catalog: max total_components")
    p.add_argument("--seed", type=int, default=42, help="catalog: shuffle seed")
    p.add_argument("--clones-dir", type=Path, default=Path("/tmp/kh_clones"))
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

    if args.catalog:
        return _procure_from_catalog(args)

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
