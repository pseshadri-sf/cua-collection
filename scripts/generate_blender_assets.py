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
import random
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


# --- harder procedural scenes (27..40) ------------------------------------

def build_boolean_diff_cube_sphere() -> None:
    """Cube with a spherical bite removed via the Boolean modifier."""
    reset_scene()
    bpy.ops.mesh.primitive_cube_add(size=2)
    cube = bpy.context.active_object
    bpy.ops.mesh.primitive_uv_sphere_add(radius=1.3, location=(1.0, 1.0, 1.0))
    sphere = bpy.context.active_object
    sphere.hide_viewport = True; sphere.hide_render = True
    mod = cube.modifiers.new("bool", "BOOLEAN")
    mod.operation = "DIFFERENCE"; mod.object = sphere
    add_camera_and_light()


def build_boolean_union_chain() -> None:
    """Three cubes united into one shape via Boolean union."""
    reset_scene()
    bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 0))
    base = bpy.context.active_object
    for i, loc in enumerate([(1.5, 0, 0.5), (-1.5, 0, 0.5)]):
        bpy.ops.mesh.primitive_cube_add(size=1.2, location=loc)
        cube = bpy.context.active_object
        cube.hide_viewport = True; cube.hide_render = True
        mod = base.modifiers.new(f"u{i}", "BOOLEAN")
        mod.operation = "UNION"; mod.object = cube
    add_camera_and_light()


def build_array_torus_circle() -> None:
    """6 toruses arranged in a circle via the Array modifier with object offset."""
    reset_scene()
    # Empty as rotation pivot
    bpy.ops.object.empty_add(location=(0, 0, 0))
    empty = bpy.context.active_object
    empty.rotation_euler[2] = math.radians(60)
    bpy.ops.mesh.primitive_torus_add(major_radius=0.4, minor_radius=0.12,
                                      location=(2.0, 0, 0))
    torus = bpy.context.active_object
    mod = torus.modifiers.new("arr", "ARRAY")
    mod.fit_type = "FIXED_COUNT"; mod.count = 6
    mod.use_object_offset = True; mod.offset_object = empty
    mod.use_relative_offset = False
    add_camera_and_light()


def build_subdiv_bevel_cube() -> None:
    """Cube with bevel + subdivision modifier stack (rounded organic cube)."""
    reset_scene()
    bpy.ops.mesh.primitive_cube_add(size=2)
    obj = bpy.context.active_object
    b = obj.modifiers.new("bev", "BEVEL"); b.width = 0.25; b.segments = 4
    s = obj.modifiers.new("sub", "SUBSURF"); s.levels = 3; s.render_levels = 3
    add_camera_and_light()


def build_kitbash_robot() -> None:
    """5-part 'robot' assembly: torso + head + two arms + base."""
    reset_scene()
    # base
    bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, -0.5))
    bpy.context.active_object.scale = (1.4, 1.4, 0.2)
    # torso
    bpy.ops.mesh.primitive_cube_add(size=1.6, location=(0, 0, 1.0))
    # head
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.55, location=(0, 0, 2.3))
    # arms
    for x in (-1.3, 1.3):
        bpy.ops.mesh.primitive_cylinder_add(radius=0.2, depth=1.6,
                                             location=(x, 0, 1.0))
        bpy.context.active_object.rotation_euler = (0, math.radians(90), 0)
    add_camera_and_light()


def build_text_3d() -> None:
    """Extruded 3D text object — exercises object-type variety."""
    reset_scene()
    bpy.ops.object.text_add(location=(-1.5, 0, 0))
    txt = bpy.context.active_object
    txt.data.body = "BLEND"; txt.data.extrude = 0.15
    add_camera_and_light()


def build_helix_curve() -> None:
    """Helical curve converted to mesh (spiral spring)."""
    reset_scene()
    bpy.ops.curve.primitive_bezier_circle_add(radius=0.3)
    circle = bpy.context.active_object
    bpy.ops.curve.primitive_nurbs_path_add(location=(0, 0, 0))
    path = bpy.context.active_object
    path.data.bevel_object = circle
    path.scale = (1.5, 1.5, 2.0)
    add_camera_and_light()


def build_displaced_plane() -> None:
    """High-poly plane with noise-style displacement modifier."""
    reset_scene()
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=40, y_subdivisions=40, size=4)
    obj = bpy.context.active_object
    tex = bpy.data.textures.new("noise", type="CLOUDS")
    m = obj.modifiers.new("disp", "DISPLACE")
    m.texture = tex; m.strength = 0.4
    add_camera_and_light()


def build_random_scatter_mixed() -> None:
    """30 random primitive objects (cube/sphere/cylinder) in a volume."""
    reset_scene()
    rng = random.Random(424242)
    primitives = [
        ("CUBE",     lambda: bpy.ops.mesh.primitive_cube_add(size=rng.uniform(0.2, 0.5))),
        ("SPHERE",   lambda: bpy.ops.mesh.primitive_uv_sphere_add(radius=rng.uniform(0.15, 0.35))),
        ("CYLINDER", lambda: bpy.ops.mesh.primitive_cylinder_add(radius=rng.uniform(0.1, 0.25),
                                                                  depth=rng.uniform(0.3, 0.8))),
    ]
    for _ in range(30):
        kind, fn = rng.choice(primitives)
        fn()
        obj = bpy.context.active_object
        obj.location = (rng.uniform(-2, 2), rng.uniform(-2, 2), rng.uniform(0, 2))
        obj.rotation_euler = (rng.uniform(0, math.pi), rng.uniform(0, math.pi),
                              rng.uniform(0, math.pi))
    add_camera_and_light()


def build_stacked_torus_tower() -> None:
    """Vertical stack of 8 toruses with decreasing radius (totem)."""
    reset_scene()
    for i in range(8):
        r = 1.0 - 0.08 * i
        bpy.ops.mesh.primitive_torus_add(major_radius=r, minor_radius=0.18,
                                          location=(0, 0, i * 0.45))
    add_camera_and_light()


def build_metaballs_blob() -> None:
    """Two metaballs merging — a smooth organic blob."""
    reset_scene()
    bpy.ops.object.metaball_add(type="BALL", location=(-0.4, 0, 0.5))
    bpy.ops.object.metaball_add(type="BALL", location=(0.4, 0, 0.5))
    add_camera_and_light()


def build_screw_helix() -> None:
    """Screw modifier on a square cross-section profile → twisted column."""
    reset_scene()
    bpy.ops.mesh.primitive_plane_add(size=0.4, location=(1, 0, 0))
    obj = bpy.context.active_object
    obj.rotation_euler = (math.radians(90), 0, 0)
    m = obj.modifiers.new("scr", "SCREW")
    m.steps = 32; m.render_steps = 32
    m.screw_offset = 4.0; m.iterations = 1
    add_camera_and_light()


def build_mirror_array_combo() -> None:
    """One cube with array → mirror modifier stack (symmetric ladder)."""
    reset_scene()
    bpy.ops.mesh.primitive_cube_add(size=0.5, location=(0.7, 0, 0))
    obj = bpy.context.active_object
    a = obj.modifiers.new("arr", "ARRAY")
    a.fit_type = "FIXED_COUNT"; a.count = 5
    a.relative_offset_displace = (1.5, 0, 0)
    mr = obj.modifiers.new("mir", "MIRROR"); mr.use_axis = (True, False, False)
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
    ("27_bool_cube_sphere",   build_boolean_diff_cube_sphere),
    ("28_bool_union_chain",   build_boolean_union_chain),
    ("29_array_torus_circle", build_array_torus_circle),
    ("30_subdiv_bevel_cube",  build_subdiv_bevel_cube),
    ("31_kitbash_robot",      build_kitbash_robot),
    ("32_text_3d",            build_text_3d),
    ("33_helix_curve",        build_helix_curve),
    ("34_displaced_plane",    build_displaced_plane),
    ("35_random_mixed",       build_random_scatter_mixed),
    ("36_torus_tower",        build_stacked_torus_tower),
    ("37_metaballs_blob",     build_metaballs_blob),
    ("38_screw_helix",        build_screw_helix),
    ("39_mirror_array",       build_mirror_array_combo),
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
