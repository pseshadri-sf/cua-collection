"""Render eval/agent_model.FCStd or .blend to a clean PNG for visualization.

Two execution paths picked automatically by interpreter:
  freecadcmd : opens .FCStd, exports to STL at env MODEL_OUT_STL (no GUI)
  blender    : opens .blend, sets up camera + EEVEE render → MODEL_OUT_PNG

Env:
  MODEL_IN   : .FCStd or .blend
  MODEL_OUT  : output PNG (blender) or STL (freecadcmd)
  RES        : "WxH" (blender), default 640x480
"""
from __future__ import annotations
import os
import sys


def _fc() -> int:
    import FreeCAD as App  # type: ignore
    import Mesh             # type: ignore
    inp = os.environ["MODEL_IN"]
    out = os.environ["MODEL_OUT"]
    d = App.openDocument(inp)
    shapes = []
    for o in d.Objects:
        if hasattr(o, "Shape") and o.Shape and not o.Shape.isNull():
            shapes.append(o)
    if not shapes:
        print(f"[fc-render] empty doc {inp}"); return 1
    Mesh.export(shapes, out)
    print(f"[fc-render] {inp} -> {out}")
    return 0


def _bl_render_stl(stl_path: str, png_out: str, res: tuple[int, int]) -> int:
    import bpy   # type: ignore
    import math
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_mesh.stl(filepath=stl_path)
    return _bl_finalize(png_out, res)


def _bl_render_blend(blend_in: str, png_out: str, res: tuple[int, int]) -> int:
    import bpy   # type: ignore
    bpy.ops.wm.open_mainfile(filepath=blend_in)
    # Strip cameras/lights to start clean (we'll add our own); keep meshes only
    for o in list(bpy.data.objects):
        if o.type in ("CAMERA", "LIGHT"):
            bpy.data.objects.remove(o, do_unlink=True)
    return _bl_finalize(png_out, res)


def _bl_finalize(png_out: str, res: tuple[int, int]) -> int:
    import bpy   # type: ignore
    import math, mathutils
    # Compute scene bbox
    mins = [+1e9, +1e9, +1e9]
    maxs = [-1e9, -1e9, -1e9]
    for o in bpy.data.objects:
        if o.type != "MESH" or not o.data: continue
        for v in o.bound_box:
            wv = o.matrix_world @ mathutils.Vector(v)
            for i, c in enumerate(wv):
                if c < mins[i]: mins[i] = c
                if c > maxs[i]: maxs[i] = c
    if mins[0] > maxs[0]:
        print("[bl-render] no mesh objects to render"); return 1
    cx = (mins[0]+maxs[0])/2; cy = (mins[1]+maxs[1])/2; cz = (mins[2]+maxs[2])/2
    span = max(maxs[0]-mins[0], maxs[1]-mins[1], maxs[2]-mins[2]) or 1.0

    # Add iso camera at 30deg elevation, 45deg azimuth, far enough to fit
    d = span * 2.5
    cam_data = bpy.data.cameras.new("cam")
    cam = bpy.data.objects.new("cam", cam_data)
    bpy.context.collection.objects.link(cam)
    az = math.radians(45); el = math.radians(30)
    cam.location = (cx + d*math.cos(el)*math.cos(az),
                    cy + d*math.cos(el)*math.sin(az),
                    cz + d*math.sin(el))
    # Aim at center
    direction = mathutils.Vector((cx, cy, cz)) - mathutils.Vector(cam.location)
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = cam

    # Add a sun light
    light_data = bpy.data.lights.new("sun", type="SUN")
    light_data.energy = 4.0
    light = bpy.data.objects.new("sun", light_data)
    bpy.context.collection.objects.link(light)
    light.rotation_euler = (math.radians(45), math.radians(30), 0)

    # World background — neutral gray
    if bpy.context.scene.world is None:
        bpy.context.scene.world = bpy.data.worlds.new("World")
    bpy.context.scene.world.use_nodes = True
    bg = bpy.context.scene.world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.18, 0.18, 0.20, 1.0)

    # Render
    s = bpy.context.scene
    s.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in {
        e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items
    } else "BLENDER_EEVEE"
    s.render.resolution_x, s.render.resolution_y = res
    s.render.image_settings.file_format = "PNG"
    s.render.filepath = png_out
    bpy.ops.render.render(write_still=True)
    print(f"[bl-render] -> {png_out}")
    return 0


if __name__ == "__main__":
    try:
        import bpy  # noqa: F401
        mode = os.environ.get("MODE", "blend")  # "blend" or "stl"
        out  = os.environ["MODEL_OUT"]
        res_s = os.environ.get("RES", "640x480").split("x")
        res = (int(res_s[0]), int(res_s[1]))
        if mode == "stl":
            raise SystemExit(_bl_render_stl(os.environ["MODEL_IN"], out, res))
        raise SystemExit(_bl_render_blend(os.environ["MODEL_IN"], out, res))
    except ImportError:
        raise SystemExit(_fc())
