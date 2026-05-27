"""Load a CAD asset in freecadcmd and emit its geometric stats as JSON.

Usage:
    freecadcmd freecad_measure.py <asset.{FCStd,step,stp,iges,...}> <stats.json>
"""
from __future__ import annotations

import json
import os
import sys

import FreeCAD as App  # type: ignore[import-not-found]


def _import_asset(path: str):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext == "fcstd":
        return App.open(path)
    doc = App.newDocument("measure_doc")
    import Part  # type: ignore[import-not-found]
    if ext in ("step", "stp", "iges", "igs"):
        # Part.read returns a Shape; wrap it in a Part::Feature.
        shape = Part.read(path)
        f = doc.addObject("Part::Feature", "Imported")
        f.Shape = shape
    elif ext == "brep":
        shape = Part.Shape()
        shape.importBrep(path)
        f = doc.addObject("Part::Feature", "Imported")
        f.Shape = shape
    elif ext == "stl":
        import Mesh    # type: ignore[import-not-found]
        Mesh.insert(path, doc.Name)
    else:
        raise RuntimeError(f"unsupported extension: {ext}")
    doc.recompute()
    return doc


def _measure(doc) -> dict:
    objs = [o for o in doc.Objects if hasattr(o, "Shape") and o.Shape and not o.Shape.isNull()]
    if not objs:
        return {
            "found": False, "object_count": 0, "object_names": [o.Name for o in doc.Objects],
            "volume": 0.0, "surface_area": 0.0, "bbox": [0, 0, 0],
            "face_count": 0, "edge_count": 0, "vertex_count": 0,
        }
    shapes = [o.Shape for o in objs]
    try:
        if len(shapes) == 1:
            combined = shapes[0]
        else:
            combined = shapes[0]
            for s in shapes[1:]:
                combined = combined.fuse(s)
    except Exception:  # noqa: BLE001
        combined = shapes[0]
    bb = combined.BoundBox
    return {
        "found": True,
        "object_count": len(objs),
        "object_names": [o.Name for o in objs],
        "volume": float(combined.Volume),
        "surface_area": float(combined.Area),
        "bbox": [float(bb.XLength), float(bb.YLength), float(bb.ZLength)],
        "face_count": len(combined.Faces),
        "edge_count": len(combined.Edges),
        "vertex_count": len(combined.Vertexes),
    }


def main(argv: list[str]) -> int:
    asset_path = os.environ.get("FCMEAS_ASSET", "")
    stats_path = os.environ.get("FCMEAS_STATS", "")
    if not (asset_path and stats_path):
        print("ERROR: FCMEAS_ASSET, FCMEAS_STATS env vars required", file=sys.stderr)
        return 2
    try:
        doc = _import_asset(asset_path)
        stats = _measure(doc)
    except Exception as exc:  # noqa: BLE001
        stats = {
            "found": False, "object_count": 0, "object_names": [],
            "volume": 0.0, "surface_area": 0.0, "bbox": [0, 0, 0],
            "face_count": 0, "edge_count": 0, "vertex_count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    open(stats_path, "w").write(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
