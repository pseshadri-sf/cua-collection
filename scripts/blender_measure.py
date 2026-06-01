"""Load a .blend file and emit its geometric stats as JSON.

Usage:
    blender -b -P blender_measure.py -- <asset.blend> <stats.json>
"""
from __future__ import annotations

import json
import sys

import bpy  # type: ignore[import-not-found]


def _args() -> list[str]:
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1:]
    return sys.argv[1:]


_CONVERTIBLE = {"FONT", "CURVE", "META", "SURFACE"}


def _convert_to_mesh() -> None:
    """Convert non-MESH renderable objects to MESH in-place.

    Required so the evaluator can measure text bodies (FONT), extruded
    curves (CURVE), metaballs (META), and NURBS surfaces (SURFACE).
    Idempotent on already-MESH objects.
    """
    bpy.ops.object.select_all(action="DESELECT")
    targets = [o for o in bpy.context.scene.objects if o.type in _CONVERTIBLE]
    if not targets:
        return
    for o in targets:
        try:
            o.select_set(True)
        except RuntimeError:
            pass
    bpy.context.view_layer.objects.active = targets[0]
    try:
        bpy.ops.object.convert(target="MESH")
    except RuntimeError as exc:
        # Some types (notably empty FONT bodies) refuse to convert; skip silently
        print(f"[measure] convert-to-MESH warning: {exc}", file=sys.stderr)


def main() -> int:
    argv = _args()
    if len(argv) < 2:
        print("usage: blender -b -P blender_measure.py -- <asset.blend> <stats.json>",
              file=sys.stderr)
        return 2
    asset_path, stats_path = argv[0], argv[1]
    try:
        bpy.ops.wm.open_mainfile(filepath=asset_path)
        stats = _measure_scene()
    except Exception as exc:  # noqa: BLE001
        stats = {
            "found": False, "object_count": 0, "object_names": [],
            "volume": 0.0, "surface_area": 0.0, "bbox": [0, 0, 0],
            "face_count": 0, "edge_count": 0, "vertex_count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    open(stats_path, "w").write(json.dumps(stats, indent=2))
    return 0


def _measure_scene() -> dict:
    # Wave-5.1 fix: convert any FONT/CURVE/META/SURFACE objects to MESH first
    # so geometric metrics include text bodies, extruded curves, metaballs, etc.
    # Without this, asset/agent both score volume=0 on text scenes.
    _convert_to_mesh()
    mesh_objs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not mesh_objs:
        return {
            "found": False, "object_count": 0,
            "object_names": [o.name for o in bpy.context.scene.objects],
            "volume": 0.0, "surface_area": 0.0, "bbox": [0, 0, 0],
            "face_count": 0, "edge_count": 0, "vertex_count": 0,
        }
    total_v = total_e = total_f = 0
    xmin = ymin = zmin = float("inf")
    xmax = ymax = zmax = float("-inf")
    total_surface = 0.0
    for obj in mesh_objs:
        mw = obj.matrix_world
        mesh = obj.data
        total_v += len(mesh.vertices)
        total_e += len(mesh.edges)
        total_f += len(mesh.polygons)
        total_surface += sum(p.area for p in mesh.polygons)
        # bound_box is an Object attribute, not Mesh.
        for v in obj.bound_box:
            wp = mw @ __import__("mathutils").Vector(v)
            xmin = min(xmin, wp.x); xmax = max(xmax, wp.x)
            ymin = min(ymin, wp.y); ymax = max(ymax, wp.y)
            zmin = min(zmin, wp.z); zmax = max(zmax, wp.z)
    import bmesh
    total_vol = 0.0
    for obj in mesh_objs:
        bm = bmesh.new()
        try:
            bm.from_object(obj, bpy.context.evaluated_depsgraph_get())
            bm.transform(obj.matrix_world)
            total_vol += abs(bm.calc_volume(signed=False))
        except Exception:  # noqa: BLE001
            pass
        finally:
            bm.free()
    return {
        "found": True,
        "object_count": len(mesh_objs),
        "object_names": [o.name for o in mesh_objs],
        "volume": float(total_vol),
        "surface_area": float(total_surface),
        "bbox": [xmax - xmin, ymax - ymin, zmax - zmin],
        "face_count": total_f,
        "edge_count": total_e,
        "vertex_count": total_v,
    }


if __name__ == "__main__":
    raise SystemExit(main())
