"""Generate small FreeCAD-compatible sample assets.

Run with FreeCAD's CLI Python:
    FREECAD_ASSET_DIR=<dir> freecadcmd generate_freecad_assets.py

The output directory is taken from $FREECAD_ASSET_DIR (env var) because
freecadcmd treats positional arguments as files-to-open, not as script args.

Creates six tiny files in $FREECAD_ASSET_DIR:
  box.FCStd, box.step
  cylinder.FCStd, cylinder.step
  bracket.FCStd, bracket.step

Tolerant of API drift across FreeCAD 0.18 -> 1.0:
    - tries Import.export() (works in CLI without GUI).
    - falls back to Part.Shape.exportStep().
"""
from __future__ import annotations

import os
import sys
import traceback

import FreeCAD as App  # type: ignore[import-not-found]
import Part  # type: ignore[import-not-found]


class _Asset:
    def __init__(self, name: str, build_fn):
        self.name = name
        self.build_fn = build_fn


def _make_box(doc):
    shape = Part.makeBox(20.0, 30.0, 10.0)
    obj = doc.addObject("Part::Feature", "Box")
    obj.Shape = shape
    return obj


def _make_cylinder(doc):
    shape = Part.makeCylinder(8.0, 25.0)
    obj = doc.addObject("Part::Feature", "Cylinder")
    obj.Shape = shape
    return obj


def _make_bracket(doc):
    base = Part.makeBox(40.0, 25.0, 4.0)
    wall = Part.makeBox(4.0, 25.0, 30.0)
    fillet_hole = Part.makeCylinder(3.0, 4.0)
    fillet_hole.translate(App.Vector(30.0, 12.5, 0.0))
    fused = base.fuse(wall).cut(fillet_hole)
    obj = doc.addObject("Part::Feature", "Bracket")
    obj.Shape = fused
    return obj


def _export_step(obj, step_path: str) -> None:
    try:
        import Import  # type: ignore[import-not-found]
        Import.export([obj], step_path)
        return
    except Exception:  # noqa: BLE001 - try Part fallback
        pass
    obj.Shape.exportStep(step_path)


def _build_one(asset: _Asset, output_dir: str) -> tuple[str, str]:
    doc_name = asset.name
    doc = App.newDocument(doc_name)
    try:
        obj = asset.build_fn(doc)
        doc.recompute()
        fcstd_path = os.path.join(output_dir, f"{asset.name}.FCStd")
        step_path = os.path.join(output_dir, f"{asset.name}.step")
        doc.saveAs(fcstd_path)
        _export_step(obj, step_path)
        return fcstd_path, step_path
    finally:
        App.closeDocument(doc_name)


def main(argv: list[str]) -> int:
    output_dir = os.environ.get("FREECAD_ASSET_DIR", "").strip()
    if not output_dir:
        print("ERROR: FREECAD_ASSET_DIR env var is required", file=sys.stderr)
        return 2
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    assets = [
        _Asset("box", _make_box),
        _Asset("cylinder", _make_cylinder),
        _Asset("bracket", _make_bracket),
    ]
    produced: list[str] = []
    failures: list[str] = []
    for asset in assets:
        try:
            fc, st = _build_one(asset, output_dir)
            produced.extend([fc, st])
            print(f"[ok] {asset.name}: {fc}, {st}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{asset.name}: {exc}")
            traceback.print_exc()

    print(f"\nProduced {len(produced)} files in {output_dir}")
    for p in produced:
        print(f"  {p}")
    if failures:
        print("Failures:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)

    # Need at least 3 loadable files total.
    return 0 if len(produced) >= 3 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
