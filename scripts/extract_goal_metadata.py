"""Pre-extract per-asset metadata for grounded prompt injection.

Run with freecadcmd (FreeCAD assets) or blender -b -P (Blender assets):

    META_ASSET=/path/to/X.step META_OUT=/tmp/X.meta.json \
        freecadcmd extract_goal_metadata.py

    META_ASSET=/path/to/X.blend META_OUT=/tmp/X.meta.json \
        blender -b -P extract_goal_metadata.py

Output JSON schema:
    {
      "asset": "/orig/path",
      "app":   "freecad" | "blender",
      "bbox_mm":          [W, D, H],
      "bbox_normalized":  [a, b, c],            // each / max(W,D,H)
      "object_count":     N,
      "face_count":       N,
      "edge_count":       N,
      "vertex_count":     N,
      "volume_mm3":       V,
      "dominant_primitive_class": "box" | "cylinder" | "sphere" | "torus" | "compound" | "revolution" | "unknown",
      "shape_descriptor": "<one-line human-readable summary>"
    }

This is what the --grounded path in vlm_client injects into the per-turn
user message so the model doesn't have to read dimensions/shape-class from
a low-res screenshot.
"""
from __future__ import annotations
import os
import sys
import json


def _classify_fc(shape, n_solids):
    """Heuristic: classify the dominant primitive from face counts & surface types.

    Run inside freecadcmd where `shape` is a Part.Shape.
    """
    try:
        if n_solids > 1:
            return "compound"
        faces = shape.Faces
        n = len(faces)
        # Count surface types
        from collections import Counter
        types = Counter()
        for f in faces:
            try:
                types[type(f.Surface).__name__] += 1
            except Exception:
                pass
        # Heuristics
        if n == 6 and types.get("Plane", 0) == 6:
            return "box"
        if n == 3 and types.get("Plane", 0) == 2 and types.get("Cylinder", 0) == 1:
            return "cylinder"
        if n == 1 and types.get("Sphere", 0) == 1:
            return "sphere"
        if n == 1 and types.get("Toroid", 0) == 1:
            return "torus"
        if any(t in types for t in ("BSplineSurface", "BezierSurface", "Cone")):
            return "revolution"
        return "unknown"
    except Exception:
        return "unknown"


def main_fc() -> int:
    import FreeCAD as App  # type: ignore[import-not-found]
    import Part            # type: ignore[import-not-found]
    asset = os.environ["META_ASSET"]
    out_path = os.environ["META_OUT"]
    try:
        ext = asset.lower().rsplit(".", 1)[-1]
        if ext in ("fcstd", "fcstd1"):
            d = App.openDocument(asset)
            objs = [o for o in d.Objects
                    if hasattr(o, "Shape") and o.Shape and not o.Shape.isNull()]
            if not objs:
                shape = None; n_solids = 0
            else:
                shape = objs[0].Shape
                for o in objs[1:]:
                    if not o.Shape.isNull(): shape = shape.fuse(o.Shape)
                n_solids = len(objs)
        else:
            shape = Part.read(asset)
            n_solids = len(shape.Solids)
        if shape is None or shape.isNull():
            raise RuntimeError("empty shape")
        bb = shape.BoundBox
        dims = [round(bb.XLength, 2), round(bb.YLength, 2), round(bb.ZLength, 2)]
        max_dim = max(dims) or 1.0
        normalized = [round(d/max_dim, 3) for d in dims]
        klass = _classify_fc(shape, n_solids)
        meta = {
            "asset":            asset,
            "app":              "freecad",
            "bbox_mm":          dims,
            "bbox_normalized":  normalized,
            "object_count":     n_solids,
            "face_count":       len(shape.Faces),
            "edge_count":       len(shape.Edges),
            "vertex_count":     len(shape.Vertexes),
            "volume_mm3":       round(shape.Volume, 2),
            "dominant_primitive_class": klass,
            "shape_descriptor": (
                f"{klass}, bbox {dims[0]}x{dims[1]}x{dims[2]} mm, "
                f"{n_solids} solid{'s' if n_solids != 1 else ''}, "
                f"{len(shape.Faces)} faces"
            ),
        }
    except Exception as exc:
        meta = {"asset": asset, "app": "freecad", "error": f"{type(exc).__name__}: {exc}"}
    with open(out_path, "w") as fh:
        json.dump(meta, fh, indent=2)
    print(f"[ok] wrote {out_path}: {meta.get('shape_descriptor', meta.get('error'))}")
    return 0


def _classify_bl(objs):
    if len(objs) > 1:
        return "compound"
    if len(objs) == 0:
        return "unknown"
    obj = objs[0]
    if obj.type != "MESH":
        return obj.type.lower()
    # Heuristic by vertex count and modifier types
    name = (obj.name or "").lower()
    mods = [m.type for m in obj.modifiers]
    if mods:
        return "modifier_stack"
    n_v = len(obj.data.vertices)
    n_f = len(obj.data.polygons)
    if n_v == 8 and n_f == 6: return "box"
    if n_v <= 100 and n_f >= 30:
        # likely cylinder/cone — circular cap
        return "cylinder"
    if n_v >= 400 and n_v <= 600 and n_f >= 480:
        return "monkey"
    if n_v > 200 and "torus" in name: return "torus"
    if n_v > 100 and n_f >= 100:
        return "sphere"
    return "unknown"


def main_bl() -> int:
    import bpy  # type: ignore[import-not-found]
    asset = os.environ["META_ASSET"]
    out_path = os.environ["META_OUT"]
    try:
        bpy.ops.wm.open_mainfile(filepath=asset)
        mesh_objs = [o for o in bpy.data.objects if o.type == "MESH"]
        non_mesh = [o for o in bpy.data.objects
                    if o.type not in ("CAMERA", "LIGHT", "MESH") and o.type not in ("EMPTY",)]
        objs = mesh_objs + non_mesh
        if not objs:
            raise RuntimeError("no objects")
        # Aggregate bbox
        xs, ys, zs = [], [], []
        for o in objs:
            if not o.data: continue
            for v in o.bound_box:
                wv = o.matrix_world @ __import__("mathutils").Vector(v)
                xs.append(wv.x); ys.append(wv.y); zs.append(wv.z)
        if not xs:
            xs = ys = zs = [0, 0]
        dims = [round(max(xs)-min(xs), 3), round(max(ys)-min(ys), 3), round(max(zs)-min(zs), 3)]
        max_dim = max(dims) or 1.0
        normalized = [round(d/max_dim, 3) for d in dims]
        total_v = sum(len(o.data.vertices) for o in mesh_objs if o.data)
        total_f = sum(len(o.data.polygons) for o in mesh_objs if o.data)
        klass = _classify_bl(objs)
        meta = {
            "asset":           asset,
            "app":             "blender",
            "bbox_mm":         dims,    # blender-units; same role as mm here
            "bbox_normalized": normalized,
            "object_count":    len(objs),
            "face_count":      total_f,
            "vertex_count":    total_v,
            "dominant_primitive_class": klass,
            "shape_descriptor": (
                f"{klass}, bbox {dims[0]}x{dims[1]}x{dims[2]} (blender-units), "
                f"{len(objs)} object{'s' if len(objs) != 1 else ''}, "
                f"{total_v} verts"
            ),
        }
    except Exception as exc:
        meta = {"asset": asset, "app": "blender", "error": f"{type(exc).__name__}: {exc}"}
    with open(out_path, "w") as fh:
        json.dump(meta, fh, indent=2)
    print(f"[ok] wrote {out_path}: {meta.get('shape_descriptor', meta.get('error'))}")
    return 0


if __name__ == "__main__":
    # Auto-detect FC vs BL by which interpreter is running us.
    try:
        import bpy  # noqa: F401
        raise SystemExit(main_bl())
    except ImportError:
        raise SystemExit(main_fc())
