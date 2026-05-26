"""Generate small Blender sample assets.

Run with Blender's CLI Python:
    BLENDER_ASSET_DIR=<dir> blender -b -P generate_blender_assets.py

Output directory comes from $BLENDER_ASSET_DIR. Each call writes 25+ .blend
files: primitives, primitive compositions, modifier-driven shapes, and a
few light/material variations.

Each scene is built into a freshly-loaded factory state so files are
deterministic in size and content. All scenes include a default camera
and a sun light so the viewport's Solid/Material shading renders visibly
when re-opened.
"""
from __future__ import annotations

import math
import os
import sys
from typing import Callable

import bpy  # type: ignore[import-not-found]

OUT_DIR = os.environ.get("BLENDER_ASSET_DIR", "").strip()


# --- helpers ---------------------------------------------------------------


def reset_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # An empty world background renders pure black; give it a neutral grey so
    # screenshots have contrast against the toolbars.
    if bpy.context.scene.world is None:
        bpy.context.scene.world = bpy.data.worlds.new("World")
    bpy.context.scene.world.use_nodes = False
    bpy.context.scene.world.color = (0.18, 0.18, 0.20)


def add_camera_and_light() -> None:
    bpy.ops.object.camera_add(
        location=(7.0, -7.0, 5.0),
        rotation=(math.radians(63), 0.0, math.radians(45)),
    )
    bpy.context.scene.camera = bpy.context.active_object
    bpy.ops.object.light_add(type="SUN", location=(4.0, -4.0, 8.0))


def save(name: str) -> str:
    path = os.path.join(OUT_DIR, f"{name}.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    return path


def material(name: str, color: tuple[float, float, float, float],
             metallic: float = 0.0, roughness: float = 0.5) -> "bpy.types.Material":
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = color
        if "Metallic" in bsdf.inputs:
            bsdf.inputs["Metallic"].default_value = metallic
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = roughness
    return mat


def apply_material(obj, mat) -> None:
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)


# --- scene builders --------------------------------------------------------


def build_cube() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_cube_add()
    add_camera_and_light()


def build_sphere() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24)
    add_camera_and_light()


def build_icosphere() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3)
    add_camera_and_light()


def build_cylinder() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_cylinder_add(vertices=48)
    add_camera_and_light()


def build_cone() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_cone_add(vertices=48)
    add_camera_and_light()


def build_torus() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_torus_add(major_radius=1.5, minor_radius=0.4)
    add_camera_and_light()


def build_monkey() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_monkey_add()
    add_camera_and_light()


def build_plane_grid() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=12, y_subdivisions=12, size=6)
    add_camera_and_light()


def build_cube_row() -> None:
    reset_scene()
    for i in range(5):
        bpy.ops.mesh.primitive_cube_add(location=(i * 2.2 - 4.4, 0.0, 0.0))
    add_camera_and_light()


def build_sphere_ring() -> None:
    reset_scene()
    n = 8
    for i in range(n):
        a = (2 * math.pi / n) * i
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=0.5, location=(2.5 * math.cos(a), 2.5 * math.sin(a), 0.0),
        )
    add_camera_and_light()


def build_pyramid_stack() -> None:
    reset_scene()
    size = 4
    for level in range(size):
        side = size - level
        for x in range(side):
            for y in range(side):
                bpy.ops.mesh.primitive_cube_add(
                    location=(x - side / 2 + 0.5, y - side / 2 + 0.5,
                              level * 2.0),
                )
    add_camera_and_light()


def build_monkey_trio() -> None:
    reset_scene()
    for i, x in enumerate((-3.0, 0.0, 3.0)):
        bpy.ops.mesh.primitive_monkey_add(location=(x, 0.0, 0.0))
        bpy.context.active_object.rotation_euler.z = math.radians(20 * i)
    add_camera_and_light()


def build_torus_array() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_torus_add(major_radius=0.8, minor_radius=0.25)
    obj = bpy.context.active_object
    mod = obj.modifiers.new("Array", type="ARRAY")
    mod.count = 5
    mod.relative_offset_displace = (1.5, 0.0, 0.0)
    add_camera_and_light()


def build_beveled_cube() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_cube_add(size=2.5)
    obj = bpy.context.active_object
    mod = obj.modifiers.new("Bevel", type="BEVEL")
    mod.width = 0.25
    mod.segments = 6
    add_camera_and_light()


def build_subdivided_sphere() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.active_object
    sub = obj.modifiers.new("Subsurf", type="SUBSURF")
    sub.levels = 3
    add_camera_and_light()


def build_mirrored_monkey() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_monkey_add(location=(1.5, 0.0, 0.0))
    obj = bpy.context.active_object
    obj.modifiers.new("Mirror", type="MIRROR")
    add_camera_and_light()


def build_screw_spring() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_circle_add(vertices=32, radius=0.3,
                                       location=(2.0, 0.0, 0.0))
    obj = bpy.context.active_object
    mod = obj.modifiers.new("Screw", type="SCREW")
    mod.screw_offset = 4.0
    mod.iterations = 4
    add_camera_and_light()


def build_metallic_cube() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    obj = bpy.context.active_object
    apply_material(obj, material("metal", (0.7, 0.7, 0.75, 1.0),
                                 metallic=1.0, roughness=0.25))
    add_camera_and_light()


def build_red_torus() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_torus_add(major_radius=1.4, minor_radius=0.4)
    obj = bpy.context.active_object
    apply_material(obj, material("red", (0.8, 0.1, 0.1, 1.0),
                                 metallic=0.1, roughness=0.4))
    add_camera_and_light()


def build_glassy_sphere() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=1.5)
    obj = bpy.context.active_object
    apply_material(obj, material("glass", (0.85, 0.92, 1.0, 1.0),
                                 metallic=0.0, roughness=0.05))
    add_camera_and_light()


def build_landscape() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=16, y_subdivisions=16, size=8)
    grid = bpy.context.active_object
    apply_material(grid, material("ground", (0.25, 0.35, 0.18, 1.0)))
    bpy.ops.mesh.primitive_uv_sphere_add(location=(0.0, 0.0, 1.2), radius=1.2)
    sphere = bpy.context.active_object
    apply_material(sphere, material("sky", (0.4, 0.6, 0.9, 1.0)))
    bpy.ops.mesh.primitive_cone_add(location=(2.5, 1.5, 0.8), depth=1.6)
    cone = bpy.context.active_object
    apply_material(cone, material("tree", (0.2, 0.5, 0.2, 1.0)))
    add_camera_and_light()


def build_random_scatter() -> None:
    reset_scene()
    import random
    random.seed(7)
    for _ in range(18):
        kind = random.choice(["cube", "sphere", "torus", "cone"])
        loc = (random.uniform(-3, 3), random.uniform(-3, 3),
               random.uniform(0, 2))
        scale = random.uniform(0.3, 0.9)
        if kind == "cube":
            bpy.ops.mesh.primitive_cube_add(size=scale, location=loc)
        elif kind == "sphere":
            bpy.ops.mesh.primitive_uv_sphere_add(radius=scale, location=loc)
        elif kind == "torus":
            bpy.ops.mesh.primitive_torus_add(major_radius=scale,
                                              minor_radius=scale * 0.3,
                                              location=loc)
        else:
            bpy.ops.mesh.primitive_cone_add(radius1=scale, depth=scale * 2,
                                            location=loc)
    add_camera_and_light()


def build_stairs() -> None:
    reset_scene()
    for i in range(8):
        bpy.ops.mesh.primitive_cube_add(
            scale=(1.5, 0.6, 0.2),
            location=(0.0, i * 1.2 - 4.0, i * 0.4),
        )
    add_camera_and_light()


def build_arch() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_cube_add(scale=(0.4, 0.4, 2.5),
                                    location=(-2.0, 0.0, 2.5))
    bpy.ops.mesh.primitive_cube_add(scale=(0.4, 0.4, 2.5),
                                    location=(2.0, 0.0, 2.5))
    bpy.ops.mesh.primitive_torus_add(major_radius=2.0, minor_radius=0.4,
                                      location=(0.0, 0.0, 5.0),
                                      rotation=(math.radians(90), 0, 0))
    add_camera_and_light()


def build_lattice_cubes() -> None:
    reset_scene()
    for x in range(-2, 3):
        for y in range(-2, 3):
            for z in range(0, 3):
                bpy.ops.mesh.primitive_cube_add(size=0.4,
                                                 location=(x, y, z))
    add_camera_and_light()


def build_textured_monkey() -> None:
    reset_scene()
    bpy.ops.mesh.primitive_monkey_add()
    obj = bpy.context.active_object
    apply_material(obj, material("orange", (0.95, 0.55, 0.05, 1.0),
                                 metallic=0.0, roughness=0.6))
    add_camera_and_light()


# --- driver ----------------------------------------------------------------


SCENES: list[tuple[str, Callable[[], None]]] = [
    ("01_cube",               build_cube),
    ("02_sphere",             build_sphere),
    ("03_icosphere",          build_icosphere),
    ("04_cylinder",           build_cylinder),
    ("05_cone",               build_cone),
    ("06_torus",              build_torus),
    ("07_monkey",             build_monkey),
    ("08_plane_grid",         build_plane_grid),
    ("09_cube_row",           build_cube_row),
    ("10_sphere_ring",        build_sphere_ring),
    ("11_pyramid_stack",      build_pyramid_stack),
    ("12_monkey_trio",        build_monkey_trio),
    ("13_torus_array",        build_torus_array),
    ("14_beveled_cube",       build_beveled_cube),
    ("15_subdivided_sphere",  build_subdivided_sphere),
    ("16_mirrored_monkey",    build_mirrored_monkey),
    ("17_screw_spring",       build_screw_spring),
    ("18_metallic_cube",      build_metallic_cube),
    ("19_red_torus",          build_red_torus),
    ("20_glassy_sphere",      build_glassy_sphere),
    ("21_landscape",          build_landscape),
    ("22_random_scatter",     build_random_scatter),
    ("23_stairs",             build_stairs),
    ("24_arch",               build_arch),
    ("25_lattice_cubes",      build_lattice_cubes),
    ("26_textured_monkey",    build_textured_monkey),
]


def main() -> int:
    if not OUT_DIR:
        print("ERROR: BLENDER_ASSET_DIR env var required", file=sys.stderr)
        return 2
    os.makedirs(OUT_DIR, exist_ok=True)
    produced: list[str] = []
    failures: list[tuple[str, str]] = []
    for name, build in SCENES:
        try:
            build()
            path = save(name)
            produced.append(path)
            print(f"[ok] {name}: {path}")
        except Exception as exc:  # noqa: BLE001 - we want a per-scene report
            failures.append((name, repr(exc)))
            print(f"[FAIL] {name}: {exc}", file=sys.stderr)
    print(f"\nProduced {len(produced)} blend files in {OUT_DIR}")
    if failures:
        print(f"Failures ({len(failures)}):", file=sys.stderr)
        for name, exc in failures:
            print(f"  {name}: {exc}", file=sys.stderr)
    return 0 if len(produced) >= 20 else 1


if __name__ == "__main__":
    raise SystemExit(main())
