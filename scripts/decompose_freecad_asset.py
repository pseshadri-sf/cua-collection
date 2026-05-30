"""Decompose a FreeCAD asset into constituent parts as standalone .step files.

Run with freecadcmd:
    FCDEC_ASSET=/path/to/asset.step FCDEC_OUT=/path/to/out_dir \
        freecadcmd decompose_freecad_asset.py

Outputs:
    out_dir/
        manifest.json
        part_00.step
        part_01.step
        ...

manifest.json schema:
    {
      "asset":      "/orig/path",
      "app":        "freecad",
      "kind":       "compound-solids" | "fcstd-objects" | "single-solid",
      "part_count": N,
      "parts": [
        {"index": 0, "name": "...", "path": "part_00.step",
         "bbox": {"x":..., "y":..., "z":...},
         "origin": {"x":..., "y":..., "z":...},
         "volume": ..., "face_count": ..., "edge_count": ..., "vertex_count": ...}
      ]
    }

For STEP/STP files that load as a Compound, each Solid becomes a part.
For STEP files that load as a single Solid, no decomposition happens
(part_count = 1). For FCStd files, each non-null Part::Feature becomes
a part using the object's Label as the name.
"""
import os
import sys
import json
import traceback

import FreeCAD as App  # type: ignore[import-not-found]
import Part            # type: ignore[import-not-found]


def _bbox_origin(shape):
    bb = shape.BoundBox
    return (
        {"x": round(bb.XLength, 3), "y": round(bb.YLength, 3), "z": round(bb.ZLength, 3)},
        {"x": round(bb.XMin, 3),    "y": round(bb.YMin, 3),    "z": round(bb.ZMin, 3)},
    )


def _stats(shape):
    return {
        "face_count":   len(shape.Faces),
        "edge_count":   len(shape.Edges),
        "vertex_count": len(shape.Vertexes),
        "volume":       round(shape.Volume, 3),
    }


def _export_step(shape, path):
    """Export a single Shape to STEP via Shape.exportStep.

    NOTE: we deliberately do NOT use Import.export([shape], path) — that
    API expects document Objects (Part::Feature), not raw Shapes; passing
    a Shape silently no-ops without raising.
    """
    try:
        shape.exportStep(path)
        return os.path.exists(path) and os.path.getsize(path) > 0
    except Exception as exc:
        print(f"[err] failed to export {path}: {exc}", file=sys.stderr)
        return False


def decompose_step(asset_path: str, out_dir: str) -> dict:
    shape = Part.read(asset_path)
    # Prefer Solids; fall back to Compounds, Shells, then single Shape.
    sub_shapes = list(shape.Solids) or list(shape.Compounds) or list(shape.Shells) or [shape]
    kind = (
        "compound-solids" if len(shape.Solids) > 1
        else "single-solid" if len(shape.Solids) == 1
        else "compound"
    )
    parts = []
    for i, sub in enumerate(sub_shapes):
        if sub.isNull():
            continue
        part_path = os.path.join(out_dir, f"part_{i:02d}.step")
        if not _export_step(sub, part_path):
            continue
        bbox, origin = _bbox_origin(sub)
        parts.append({
            "index": i,
            "name": f"part_{i:02d}",   # STEP files rarely carry meaningful names
            "path": os.path.basename(part_path),
            "bbox": bbox,
            "origin": origin,
            **_stats(sub),
        })
    return {"kind": kind, "parts": parts}


def decompose_fcstd(asset_path: str, out_dir: str) -> dict:
    doc = App.openDocument(asset_path)
    parts = []
    for i, obj in enumerate(doc.Objects):
        if not hasattr(obj, "Shape"):
            continue
        sh = obj.Shape
        if sh is None or sh.isNull():
            continue
        # Skip helper/base-plane objects
        if obj.TypeId in {"App::Origin", "App::Plane", "App::Line", "App::Part"}:
            continue
        part_path = os.path.join(out_dir, f"part_{i:02d}.step")
        if not _export_step(sh, part_path):
            continue
        bbox, origin = _bbox_origin(sh)
        # Sanitise the label for use as a filename / identifier
        name = (getattr(obj, "Label", obj.Name) or obj.Name).replace(" ", "_")
        parts.append({
            "index": i,
            "name": name,
            "path": os.path.basename(part_path),
            "bbox": bbox,
            "origin": origin,
            **_stats(sh),
        })
    return {"kind": "fcstd-objects", "parts": parts}


def main() -> int:
    asset = os.environ.get("FCDEC_ASSET")
    out_dir = os.environ.get("FCDEC_OUT")
    if not asset or not out_dir:
        print("ERROR: set FCDEC_ASSET and FCDEC_OUT env vars", file=sys.stderr)
        return 2
    if not os.path.exists(asset):
        print(f"ERROR: asset not found: {asset}", file=sys.stderr)
        return 2
    os.makedirs(out_dir, exist_ok=True)

    ext = os.path.splitext(asset)[1].lower()
    try:
        if ext in (".fcstd", ".fcstd1"):
            result = decompose_fcstd(asset, out_dir)
        else:
            result = decompose_step(asset, out_dir)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        return 3

    manifest = {
        "asset": asset,
        "app": "freecad",
        "kind": result["kind"],
        "part_count": len(result["parts"]),
        "parts": result["parts"],
    }
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"[ok] {asset} → {manifest['part_count']} parts ({result['kind']})")
    print(f"      manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
