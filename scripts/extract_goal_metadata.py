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
        # Multi-planar prism (hex/pent/oct prism, with bore optional)
        if types.get("Plane", 0) >= 5 and types.get("Cylinder", 0) <= 2 \
                and not any(t in types for t in ("BSplineSurface", "BezierSurface", "Toroid", "Sphere")):
            return "prism"
        if any(t in types for t in ("BSplineSurface", "BezierSurface", "SurfaceOfRevolution")):
            return "revolution"
        if "Cone" in types:
            return "cone"
        return "unknown"
    except Exception:
        return "unknown"


def _surface_taxonomy_fc(shape):
    """Wave-5 F1: per-face surface-type histogram + representative parameters.

    Returns:
      {"counts": {type: n}, "samples": [{type, ...params}, ...]}
    """
    from collections import Counter
    counts = Counter()
    seen_types = set()
    samples = []
    for f in shape.Faces:
        try:
            s = f.Surface
            cls = type(s).__name__
            counts[cls] += 1
            if cls in seen_types or len(samples) >= 10:
                continue
            seen_types.add(cls)
            entry = {"type": cls, "area": round(f.Area, 2)}
            if cls == "Cylinder":
                entry["radius"] = round(s.Radius, 3)
                entry["axis"]   = [round(s.Axis.x,3), round(s.Axis.y,3), round(s.Axis.z,3)]
            elif cls == "Sphere":
                entry["radius"] = round(s.Radius, 3)
            elif cls == "Toroid":
                entry["major_radius"] = round(s.MajorRadius, 3)
                entry["minor_radius"] = round(s.MinorRadius, 3)
            elif cls == "Cone":
                entry["semi_angle"] = round(s.SemiAngle, 4)
                entry["radius"]     = round(s.Radius, 3)
                entry["axis"]       = [round(s.Axis.x,3), round(s.Axis.y,3), round(s.Axis.z,3)]
            elif cls == "Plane":
                n = s.Axis
                entry["normal"] = [round(n.x,3), round(n.y,3), round(n.z,3)]
            elif cls in ("BSplineSurface", "BezierSurface"):
                try:
                    entry["u_degree"] = s.UDegree
                    entry["v_degree"] = s.VDegree
                except Exception: pass
            samples.append(entry)
        except Exception: pass
    return {"counts": dict(counts), "samples": samples}


def _curve_taxonomy_fc(shape):
    """Wave-5 F2: edge-curve histogram."""
    from collections import Counter
    counts = Counter()
    for e in shape.Edges:
        try:
            counts[type(e.Curve).__name__] += 1
        except Exception:
            counts["unknown"] += 1
    return dict(counts)


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
            # Wave-5 F1 + F2
            "surface_taxonomy": _surface_taxonomy_fc(shape),
            "curve_taxonomy":   _curve_taxonomy_fc(shape),
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


def _bl_modifier_summary(obj):
    """Wave-5 B3: dump modifier stack with construction parameters."""
    out = []
    for m in obj.modifiers:
        e = {"name": m.name, "type": m.type}
        try:
            if m.type == "ARRAY":
                e.update(count=m.count, fit_type=m.fit_type,
                         relative_offset=[round(v, 4) for v in m.relative_offset_displace],
                         constant_offset=[round(v, 4) for v in m.constant_offset_displace])
            elif m.type == "MIRROR":
                e.update(use_axis=list(m.use_axis))
            elif m.type == "BOOLEAN":
                e.update(operation=m.operation,
                         operand=(m.object.name if m.object else None))
            elif m.type == "BEVEL":
                e.update(width=round(m.width, 4), segments=m.segments,
                         limit_method=m.limit_method, affect=m.affect)
            elif m.type == "SUBSURF":
                e.update(levels=m.levels, subdivision_type=m.subdivision_type)
            elif m.type == "SOLIDIFY":
                e.update(thickness=round(m.thickness, 4), offset=round(m.offset, 4))
            elif m.type == "SCREW":
                e.update(screw_offset=round(m.screw_offset, 4),
                         angle=round(m.angle, 4), steps=m.steps, axis=m.axis)
            elif m.type == "DISPLACE":
                e.update(strength=round(m.strength, 4), midlevel=round(m.mid_level, 4))
            elif m.type == "WAVE":
                e.update(height=round(m.height, 4), width=round(m.width, 4))
        except Exception: pass
        out.append(e)
    return out


def _bl_object_meta(obj):
    """Wave-5 B1+B2: per-object rotation, parent, and type-specific fields."""
    import math
    e = {
        "name":     obj.name,
        "type":     obj.type,
        "location": [round(c, 4) for c in obj.location],
        "rotation_euler": [round(c, 4) for c in obj.rotation_euler],
        "rotation_deg":   [round(math.degrees(c), 1) for c in obj.rotation_euler],
        "scale":    [round(c, 4) for c in obj.scale],
        "dimensions": [round(c, 4) for c in obj.dimensions],
        "parent":   (obj.parent.name if obj.parent else None),
    }
    if obj.modifiers:
        e["modifiers"] = _bl_modifier_summary(obj)
    # Type-specific data blocks
    if obj.type == "FONT" and obj.data:
        d = obj.data
        e["text"] = {
            "body":    (d.body or "")[:200],
            "size":    round(d.size, 4),
            "extrude": round(d.extrude, 4),
            "font":    (d.font.name if d.font else None),
            "align_x": d.align_x, "align_y": d.align_y,
        }
    elif obj.type == "CURVE" and obj.data:
        d = obj.data
        splines = []
        for sp in d.splines[:5]:
            splines.append({
                "type": sp.type,
                "n_points": (len(sp.bezier_points) if sp.type == "BEZIER"
                             else len(sp.points)),
                "cyclic": sp.use_cyclic_u,
                "order_u": sp.order_u,
            })
        e["curve"] = {"dimensions": d.dimensions,
                       "extrude": round(getattr(d, "extrude", 0.0), 4),
                       "bevel_depth": round(getattr(d, "bevel_depth", 0.0), 4),
                       "splines": splines}
    elif obj.type == "META" and obj.data:
        e["metaball"] = {"elements": len(obj.data.elements)}
    elif obj.type == "SURFACE" and obj.data:
        e["surface"] = {"n_splines": len(obj.data.splines)}
    return e


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
        # Wave-5 B1+B2+B3: per-object enriched info
        object_types_present = sorted({o.type for o in objs})
        objects_meta = [_bl_object_meta(o) for o in objs[:30]]
        meta = {
            "asset":           asset,
            "app":             "blender",
            "bbox_mm":         dims,    # blender-units; same role as mm here
            "bbox_normalized": normalized,
            "object_count":    len(objs),
            "face_count":      total_f,
            "vertex_count":    total_v,
            "dominant_primitive_class": klass,
            "object_types_present": object_types_present,
            "objects_meta": objects_meta,
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


def main_kicad() -> int:
    """KiCad (pcbnew) goal-metadata extractor. Run with the system python3 that
    has the pcbnew module (KiCad install). Emits bbox + the kicad{} block the
    planner grounding uses (footprint placements, nets, layer counts)."""
    import pcbnew  # type: ignore[import-not-found]
    asset = os.environ["META_ASSET"]
    out_path = os.environ["META_OUT"]
    try:
        board = pcbnew.LoadBoard(asset)
        tomm = pcbnew.ToMM

        # Board outline bbox (Edge.Cuts); fall back to the full bbox.
        try:
            bb = board.GetBoardEdgesBoundingBox()
            if bb.GetWidth() == 0 and bb.GetHeight() == 0:
                bb = board.GetBoundingBox()
        except Exception:
            bb = board.GetBoundingBox()
        w_mm = round(tomm(bb.GetWidth()), 2)
        d_mm = round(tomm(bb.GetHeight()), 2)

        footprints = []
        pad_count = 0
        for fp in board.GetFootprints():
            pos = fp.GetPosition()
            pads = list(fp.Pads())
            pad_count += len(pads)
            footprints.append({
                "ref": fp.GetReference(),
                "name": fp.GetFPIDAsString(),
                "at": [round(tomm(pos.x), 2), round(tomm(pos.y), 2)],
                "rot": round(fp.GetOrientationDegrees(), 1),
                "layer": "B.Cu" if fp.IsFlipped() else "F.Cu",
            })

        # Net names (skip the unconnected net 0). FindNet accepts a netcode.
        nets = []
        for code in range(1, board.GetNetCount()):
            try:
                ni = board.FindNet(code)
                if ni is not None:
                    nm = ni.GetNetname()
                    if nm:
                        nets.append(nm)
            except Exception:
                pass

        layer_count = board.GetCopperLayerCount()
        net_count = max(0, board.GetNetCount() - 1)
        meta = {
            "asset": asset,
            "app": "kicad",
            "bbox_mm": [w_mm, d_mm, 0],
            "object_count": len(footprints),
            "kicad": {
                "footprint_count": len(footprints),
                "net_count": net_count,
                "layer_count": layer_count,
                "pad_count": pad_count,
                "outline_bbox_mm": [w_mm, d_mm],
                "footprints": footprints,
                "nets": nets,
            },
            "shape_descriptor": (
                f"PCB: {len(footprints)} footprints, {net_count} nets, "
                f"{layer_count} copper layers, {w_mm}x{d_mm} mm board"
            ),
        }
    except Exception as exc:  # noqa: BLE001
        meta = {"asset": asset, "app": "kicad", "error": f"{type(exc).__name__}: {exc}"}
    with open(out_path, "w") as fh:
        json.dump(meta, fh, indent=2)
    print(f"[ok] wrote {out_path}: {meta.get('shape_descriptor', meta.get('error'))}")
    return 0


if __name__ == "__main__":
    # Auto-detect engine by which interpreter is running us:
    # blender (bpy) -> KiCad (pcbnew) -> FreeCAD (freecadcmd default).
    # META_APP can force a branch.
    forced = os.environ.get("META_APP", "").lower()
    if forced == "kicad":
        raise SystemExit(main_kicad())
    if forced == "blender":
        raise SystemExit(main_bl())
    if forced == "freecad":
        raise SystemExit(main_fc())
    try:
        import bpy  # noqa: F401
        raise SystemExit(main_bl())
    except ImportError:
        pass
    try:
        import pcbnew  # noqa: F401
        raise SystemExit(main_kicad())
    except ImportError:
        raise SystemExit(main_fc())
