"""Reconstruct an agent's Blender scene from its emitted Python chunks.

Usage:
    blender -b -P blender_reconstruct.py -- <chunks.json> <out.blend> <stats.json>
"""
from __future__ import annotations

import json
import sys
import traceback

import bpy  # type: ignore[import-not-found]


def _args() -> list[str]:
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1:]
    return sys.argv[1:]


def run() -> int:
    argv = _args()
    if len(argv) < 3:
        print("usage: blender -b -P blender_reconstruct.py -- "
              "<chunks.json> <out.blend> <stats.json>", file=sys.stderr)
        return 2
    chunks_path, out_path, stats_path = argv[0], argv[1], argv[2]
    chunks = json.loads(open(chunks_path).read())

    # Start from Blender's factory default scene (matches what the agent saw).
    bpy.ops.wm.read_factory_settings(use_empty=False)

    ns = {"bpy": bpy}
    failures: list[str] = []
    for i, chunk in enumerate(chunks):
        try:
            exec(chunk, ns)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"chunk {i}: {type(exc).__name__}: {exc}")

    # Wave-5.1: convert FONT/CURVE/META/SURFACE to MESH so the evaluator can
    # measure text bodies, extruded curves, metaballs, NURBS surfaces.
    _convert_to_mesh()

    stats = _measure_scene()
    stats["chunks"] = len(chunks)
    stats["replay_failures"] = failures[:20]

    try:
        bpy.ops.wm.save_as_mainfile(filepath=out_path)
    except Exception as exc:  # noqa: BLE001
        stats.setdefault("error", f"save failed: {exc}")

    open(stats_path, "w").write(json.dumps(stats, indent=2))
    return 0


_CONVERTIBLE = {"FONT", "CURVE", "META", "SURFACE"}


def _convert_to_mesh() -> None:
    """Convert non-MESH renderable objects to MESH (Wave-5.1 evaluator fix)."""
    bpy.ops.object.select_all(action="DESELECT")
    targets = [o for o in bpy.context.scene.objects if o.type in _CONVERTIBLE]
    if not targets:
        return
    for o in targets:
        try: o.select_set(True)
        except RuntimeError: pass
    bpy.context.view_layer.objects.active = targets[0]
    try:
        bpy.ops.object.convert(target="MESH")
    except RuntimeError as exc:
        print(f"[reconstruct] convert-to-MESH warning: {exc}", file=sys.stderr)


def _measure_scene() -> dict:
    mesh_objs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    names = [o.name for o in bpy.context.scene.objects]
    if not mesh_objs:
        return {
            "found": False, "object_count": 0, "object_names": names,
            "volume": 0.0, "surface_area": 0.0, "bbox": [0, 0, 0],
            "face_count": 0, "edge_count": 0, "vertex_count": 0,
        }
    # Aggregate over all meshes in the scene.
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
    bbox = [xmax - xmin, ymax - ymin, zmax - zmin]
    # Volume via bmesh aggregate (skips non-watertight contributions).
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
        "bbox": [float(x) for x in bbox],
        "face_count": total_f,
        "edge_count": total_e,
        "vertex_count": total_v,
    }


if __name__ == "__main__":
    raise SystemExit(run())
