"""CLI entry point for the Blender smoketest."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cua_smoketest.blender_smoketest import BlenderSmoketest  # noqa: E402
from cua_smoketest.paths import SmoketestPaths                # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="blender-smoketest",
        description="Blender GUI smoketest harness (CPU rendering via Mesa).",
    )
    parser.add_argument("--generator", type=str, default=str(
        _REPO_ROOT / "scripts" / "generate_blender_assets.py"))
    parser.add_argument("--max-assets", type=int, default=20)
    parser.add_argument("--asset-offset", type=int, default=0,
                        help="Skip the first N assets (use to separate phases).")
    parser.add_argument("--mode", choices=("process", "cursor"), default="process")
    parser.add_argument("--skip-generation", action="store_true")
    parser.add_argument("--screenshot-prefix", type=str, default="")
    args = parser.parse_args(argv)

    paths = SmoketestPaths.for_app("blender")
    paths.ensure()
    generator = Path(args.generator).expanduser().resolve()
    if not generator.exists():
        print(f"ERROR: generator script not found: {generator}", file=sys.stderr)
        return 2

    smoketest = BlenderSmoketest(
        paths=paths, generator_script=generator,
        max_assets=args.max_assets,
        mode=args.mode,
        skip_generation=args.skip_generation,
        screenshot_prefix=args.screenshot_prefix,
        asset_offset=args.asset_offset,
    )
    result = smoketest.run()

    print("\n========== BLENDER SMOKETEST REPORT ==========")
    print(f"OS:          {result.env.os_id} {result.env.os_version} ({result.env.arch})")
    print(f"DISPLAY:     {result.display.display} (reused={result.display.reused})")
    print(f"Blender:     {result.env.tools.get('blender') or ''}")
    print(f"Mode:        {smoketest.mode}")
    print(f"Assets in tree: {len(result.assets.all_files)}")
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
