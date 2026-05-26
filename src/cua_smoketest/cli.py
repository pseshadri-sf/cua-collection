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
    args = parser.parse_args(argv)

    paths = SmoketestPaths.default()
    paths.ensure()
    generator = Path(args.generator).expanduser().resolve()
    if not generator.exists():
        print(f"ERROR: generator script not found: {generator}", file=sys.stderr)
        return 2

    smoketest = Smoketest(paths=paths, generator_script=generator,
                          max_assets=args.max_assets)
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
