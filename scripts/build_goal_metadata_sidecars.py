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
    print(f"\nDONE: ok={n_ok} skip={n_skip} miss={n_miss} err={n_err} total={len(goal_pngs)}")
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
