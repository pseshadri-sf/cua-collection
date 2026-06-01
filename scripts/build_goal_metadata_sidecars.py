"""Pre-build `<goal>.meta.json` sidecars for every goal PNG in a directory.

Resolves each goal PNG back to its source CAD asset, then dispatches the
appropriate kernel (freecadcmd or blender -b -P) to run
scripts/extract_goal_metadata.py.

Usage:
    uv run python scripts/build_goal_metadata_sidecars.py \\
        --screenshots-dir /home/ubuntu/cua_gui_smoketest/screenshots \\
        --app freecad

    uv run python scripts/build_goal_metadata_sidecars.py \\
        --screenshots-dir /home/ubuntu/cua_blender_smoketest/screenshots \\
        --app blender

Idempotent: skips PNGs that already have a fresh sidecar (mtime newer than
the source asset).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cua_smoketest.agent.evaluator import (   # noqa: E402
    resolve_freecad_goal_asset, resolve_blender_goal_asset,
)

_EXTRACTOR = _REPO_ROOT / "scripts" / "extract_goal_metadata.py"
_DECOMPOSE_FC = _REPO_ROOT / "scripts" / "decompose_freecad_asset.py"
_DECOMPOSE_BL = _REPO_ROOT / "scripts" / "decompose_blender_asset.py"


def _maybe_decompose(sidecar: Path, asset: Path, app: str,
                     max_parts: int, freecadcmd: str, blender: str) -> None:
    """If sidecar's object_count > 1, run the matching decomposer and merge
    its parts list into the sidecar (capped at `max_parts`, sorted by volume).
    """
    import json as _json
    try:
        meta = _json.loads(sidecar.read_text())
    except (OSError, _json.JSONDecodeError):
        return
    if meta.get("object_count", 0) <= 1:
        return
    if meta.get("parts"):
        return  # already present
    out_dir = asset.parent.parent / "decomposed" / asset.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    if app == "freecad":
        env["FCDEC_ASSET"] = str(asset); env["FCDEC_OUT"] = str(out_dir)
        cmd = [freecadcmd, str(_DECOMPOSE_FC)]
    else:
        env["BLDEC_ASSET"] = str(asset); env["BLDEC_OUT"] = str(out_dir)
        cmd = [blender, "-b", "-noaudio", "-P", str(_DECOMPOSE_BL)]
    try:
        subprocess.run(cmd, env=env, timeout=240,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"  [decompose err] {asset.name}: {type(exc).__name__}")
        return
    manifest = out_dir / "manifest.json"
    if not manifest.exists():
        return
    try:
        m = _json.loads(manifest.read_text())
    except _json.JSONDecodeError:
        return
    parts = m.get("parts") or []
    # Sort by volume desc (FC has 'volume', BL has dimensions but no vol — approximate)
    def _vol_of(p):
        if "volume" in p: return float(p["volume"])
        dims = p.get("dimensions") or [0, 0, 0]
        return dims[0] * dims[1] * dims[2]
    parts_sorted = sorted(parts, key=_vol_of, reverse=True)
    kept = parts_sorted[:max_parts]
    truncated = len(parts) - len(kept)

    # Normalize each part's record to a compact common schema
    flat = []
    for p in kept:
        if app == "freecad":
            bb = p.get("bbox") or {}
            orig = p.get("origin") or {}
            flat.append({
                "index": p["index"], "name": p.get("name", f"part_{p['index']:02d}"),
                "bbox":   [bb.get("x", 0), bb.get("y", 0), bb.get("z", 0)],
                "origin": [orig.get("x", 0), orig.get("y", 0), orig.get("z", 0)],
                "volume": p.get("volume", 0),
                "face_count": p.get("face_count", 0),
            })
        else:
            dims = p.get("dimensions") or [0, 0, 0]
            loc  = p.get("location")   or [0, 0, 0]
            flat.append({
                "index": p["index"], "name": p.get("name", f"part_{p['index']:02d}"),
                "bbox":   [round(dims[0], 3), round(dims[1], 3), round(dims[2], 3)],
                "origin": [round(loc[0], 3),  round(loc[1], 3),  round(loc[2], 3)],
                "vertex_count": p.get("vertex_count", 0),
                "face_count":   p.get("face_count", 0),
            })
    meta["parts"] = flat
    meta["parts_kind"] = m.get("kind", "?")
    if truncated > 0:
        meta["parts_truncated"] = truncated
    sidecar.write_text(_json.dumps(meta, indent=2))
    print(f"        + decomposed: {len(flat)}/{len(parts)} parts merged")


def _resolve(png: Path):
    # Auto-pick by which screenshots dir the path lives under.
    if "blender" in str(png):
        return ("blender", resolve_blender_goal_asset(png))
    return ("freecad", resolve_freecad_goal_asset(png))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--screenshots-dir",
                     help="Process every [A-Z]_*loaded_*.png in this dir.")
    src.add_argument("--jobs-file",
                     help="Process only the goal_path entries in this JSONL.")
    p.add_argument("--app", choices=("freecad", "blender"),
                   help="Force kernel (default: auto-detect per-png).")
    p.add_argument("--force", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--freecadcmd", default="freecadcmd")
    p.add_argument("--blender",    default="blender")
    p.add_argument("--decompose", action="store_true",
                   help="When object_count > 1, also run the per-part "
                        "decomposer and embed the parts list into the "
                        "sidecar. Enables Wave-4.1 per-part prompt injection.")
    p.add_argument("--decompose-max-parts", type=int, default=12,
                   help="Cap parts list at N largest-by-volume parts; "
                        "remainder summarised as '... and M more parts'.")
    args = p.parse_args(argv)

    if args.screenshots_dir:
        sd = Path(args.screenshots_dir)
        if not sd.is_dir():
            print(f"ERROR: not a directory: {sd}", file=sys.stderr); return 2
        import re as _re
        goal_pngs = sorted(p for p in sd.iterdir()
                           if _re.match(r"^[A-Z]_\d+_loaded_.+\.png$", p.name))
    else:
        import json as _json
        jobs_path = Path(args.jobs_file)
        goal_pngs = []
        seen = set()
        with jobs_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line: continue
                gp = Path(_json.loads(line)["goal_path"])
                if str(gp) in seen: continue
                seen.add(str(gp))
                goal_pngs.append(gp)
    if args.limit:
        goal_pngs = goal_pngs[:args.limit]
    if not goal_pngs:
        print("No goal PNGs to process", file=sys.stderr); return 1

    n_ok = n_skip = n_miss = n_err = 0
    for png in goal_pngs:
        sidecar = png.with_suffix(".meta.json")
        if args.app:
            asset = (resolve_freecad_goal_asset(png) if args.app == "freecad"
                     else resolve_blender_goal_asset(png))
            app = args.app
        else:
            app, asset = _resolve(png)
        if asset is None or not asset.exists():
            print(f"[miss] no source asset for {png.name}")
            n_miss += 1; continue
        if sidecar.exists() and not args.force \
                and sidecar.stat().st_mtime >= asset.stat().st_mtime:
            n_skip += 1; continue

        env = os.environ.copy()
        env["META_ASSET"] = str(asset)
        env["META_OUT"]   = str(sidecar)
        if app == "freecad":
            cmd = [args.freecadcmd, str(_EXTRACTOR)]
        else:
            cmd = [args.blender, "-b", "-noaudio", "-P", str(_EXTRACTOR)]
        try:
            r = subprocess.run(cmd, env=env, timeout=120,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except subprocess.TimeoutExpired:
            print(f"[err ] timeout: {asset.name}"); n_err += 1; continue
        if r.returncode != 0 or not sidecar.exists():
            print(f"[err ] {asset.name}: rc={r.returncode} {r.stderr.decode()[:200]}")
            n_err += 1; continue
        n_ok += 1
        print(f"[ok  ] {png.name} -> {sidecar.name}")

        if args.decompose:
            _maybe_decompose(sidecar, asset, app, args.decompose_max_parts,
                             args.freecadcmd, args.blender)
    print(f"\nDONE: ok={n_ok} skip={n_skip} miss={n_miss} err={n_err} total={len(goal_pngs)}")
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
