from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .paths import SmoketestPaths
from .smoketest import Smoketest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cua-smoketest",
        description="Run FreeCAD GUI smoketest (Blender installed but not exercised).",
    )
    parser.add_argument(
        "--generator", required=True,
        help="Path to the freecadcmd-executable asset generator script.",
    )
    parser.add_argument("--max-assets", type=int, default=3)
    parser.add_argument("--download", type=int, default=0,
                        help="Download N small CAD samples from FreeCAD-library before running.")
    parser.add_argument("--download-cache", type=str, default="",
                        help="Where to keep the shallow library clone (default ~/.cache/freecad-library).")
    parser.add_argument("--screenshot-prefix", type=str, default="",
                        help="Prepended to all screenshot filenames (use to separate phases).")
    parser.add_argument("--exclude-from", type=str, default="",
                        help="Path to a file listing screenshot basenames to skip during download.")
    parser.add_argument("--mode", choices=("process", "cursor"), default="process",
                        help="Asset loading mode: process (one FreeCAD per asset, xdotool keys) "
                             "or cursor (one FreeCAD, pyautogui mouse-driven File>Open).")
    parser.add_argument("--skip-generation", action="store_true",
                        help="Skip freecadcmd asset generation; just discover existing files.")
    parser.add_argument("--only-downloaded", action="store_true",
                        help="Run only against the assets downloaded in this invocation "
                             "(ignored if --download is 0).")
    args = parser.parse_args(argv)

    paths = SmoketestPaths.default()
    paths.ensure()
    generator = Path(args.generator).expanduser().resolve()
    if not generator.exists():
        print(f"ERROR: generator script not found: {generator}", file=sys.stderr)
        return 2

    exclude: set[str] = set()
    if args.exclude_from:
        ef = Path(args.exclude_from).expanduser()
        if ef.exists():
            exclude = {ln.strip() for ln in ef.read_text().splitlines() if ln.strip()}

    smoketest = Smoketest(
        paths=paths, generator_script=generator,
        max_assets=args.max_assets,
        download_count=args.download,
        download_cache=Path(args.download_cache).expanduser() if args.download_cache else None,
        screenshot_prefix=args.screenshot_prefix,
        exclude_basenames=exclude,
        mode=args.mode,
        skip_generation=args.skip_generation,
        only_downloaded=args.only_downloaded,
    )
    result = smoketest.run()

    print("\n========== SMOKETEST REPORT ==========")
    print(f"OS:               {result.env.os_id} {result.env.os_version} ({result.env.arch})")
    print(f"DISPLAY:          {result.display.display} (reused={result.display.reused})")
    print(f"FreeCAD:          {(result.env.tools.get('freecad') or '')} (cmd: {result.env.tools.get('freecadcmd') or ''})")
    print(f"Blender:          {result.env.tools.get('blender') or ''}")
    print(f"Assets ({len(result.assets.all_files)}):")
    for a in result.assets.all_files:
        print(f"  - {a}")
    print(f"Screenshots ({len(result.screenshots)}):")
    for s in result.screenshots:
        print(f"  - {s}")
    print(f"Log: {result.log_path}")
    print(f"Success: {result.success}")
    if result.error:
        print(f"Error: {result.error}")
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
