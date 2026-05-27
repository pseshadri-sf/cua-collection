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
        for v in mesh.bound_box:
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
