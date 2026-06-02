"""Render a multi-view atlas for a goal asset (S3 from text-to-cad scope).

For each goal asset, produces a single PNG atlas with 4 views in a 2x2 grid:

    +----------+----------+
    |   ISO    |  FRONT   |
    +----------+----------+
    |   TOP    |  RIGHT   |
    +----------+----------+

If the asset's sidecar reports `bbox_volume / mesh_volume > 1.5` (hollow), we
extend to a 2x3 layout adding a SECTION_Y view (cut across the asset's middle
in Y to reveal interior cavities).

This addresses the FC Wave-6 silhouette-ambiguity ceiling: one iso view makes
sprockets/cans/hinges/cups indistinguishable from boxes. The top + section
views directly reveal tooth count, hole pattern, and hollow interiors.

The agent sees a SINGLE goal image, the atlas, so no API change downstream.
View labels are burnt into the bottom-left of each tile so the VLM can
identify which camera is which.

Cached per asset stem under /tmp/multiview_cache/<stem>.png; rerun with
--force to regenerate.

Usage:
    python render_goal_multiview.py --asset <path> --sidecar <path>.meta.json [--force]
    python render_goal_multiview.py --jobs-file <jobs.jsonl>
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


_REPO = Path(__file__).resolve().parent.parent
_CACHE = Path("/tmp/multiview_cache")
_TILE = (640, 480)
_VIEWS_DEFAULT = ("iso", "front", "top", "right")
_VIEWS_HOLLOW = ("iso", "front", "top", "right", "section_y")


def _is_hollow(sidecar: dict) -> bool:
    bbox = sidecar.get("bbox_mm")
    vol = sidecar.get("volume_mm3") or 0.0
    if not bbox or not vol:
        return False
    bbox_vol = bbox[0] * bbox[1] * bbox[2]
    if bbox_vol <= 0:
        return False
    return (bbox_vol / vol) > 1.5


# ─────────────────────────────────────────────────────────────────────────
# Blender-side render script (runs under `blender -b -P`)
# ─────────────────────────────────────────────────────────────────────────
_BL_RENDER = '''\
import bpy, mathutils, math, os, sys

MODE = os.environ["MV_MODE"]           # "stl" or "blend"
IN   = os.environ["MV_IN"]
OUT  = os.environ["MV_OUT"]            # output PNG path
VIEW = os.environ["MV_VIEW"]           # iso|front|top|right|section_y
RES  = os.environ.get("MV_RES", "640x480").split("x")
W, H = int(RES[0]), int(RES[1])

bpy.ops.wm.read_factory_settings(use_empty=True)
if MODE == "stl":
    try:
        bpy.ops.wm.stl_import(filepath=IN)        # Blender 4.x
    except Exception:
        bpy.ops.import_mesh.stl(filepath=IN)      # Blender 3.x
elif MODE == "blend":
    bpy.ops.wm.open_mainfile(filepath=IN)
    for o in list(bpy.data.objects):
        if o.type in ("CAMERA", "LIGHT"):
            bpy.data.objects.remove(o, do_unlink=True)
    # Convert non-mesh to mesh so they render
    for o in list(bpy.data.objects):
        if o.type in ("FONT", "CURVE", "META", "SURFACE") and o.data:
            try:
                bpy.context.view_layer.objects.active = o
                o.select_set(True)
                bpy.ops.object.convert(target="MESH")
                o.select_set(False)
            except Exception: pass
else:
    sys.exit(1)

# Scene bbox
mins = [+1e9]*3; maxs = [-1e9]*3
for o in bpy.data.objects:
    if o.type != "MESH" or not o.data: continue
    for v in o.bound_box:
        wv = o.matrix_world @ mathutils.Vector(v)
        for i,c in enumerate(wv):
            if c < mins[i]: mins[i] = c
            if c > maxs[i]: maxs[i] = c
if mins[0] > maxs[0]:
    sys.exit(1)
cx,cy,cz = [(mins[i]+maxs[i])/2 for i in range(3)]
span = max(maxs[i]-mins[i] for i in range(3)) or 1.0

# Camera per view
cam_data = bpy.data.cameras.new("cam")
cam = bpy.data.objects.new("cam", cam_data)
bpy.context.collection.objects.link(cam)

if VIEW == "iso":
    d = span * 2.5
    az, el = math.radians(45), math.radians(30)
    cam.location = (cx + d*math.cos(el)*math.cos(az),
                    cy + d*math.cos(el)*math.sin(az),
                    cz + d*math.sin(el))
elif VIEW == "front":
    cam.location = (cx, cy - span*2.5, cz)
elif VIEW == "top":
    cam.location = (cx, cy, cz + span*2.5)
elif VIEW == "right":
    cam.location = (cx + span*2.5, cy, cz)
elif VIEW == "section_y":
    # Apply a half-space cut: hide everything with y > cy via a boolean.
    # Simpler: place a clip-plane via a large cutting cube boolean-diff.
    cut_size = span * 4
    bpy.ops.mesh.primitive_cube_add(size=cut_size,
        location=(cx, cy + cut_size/2 + 1e-3, cz))
    cutter = bpy.context.active_object
    cutter.name = "__cutter__"
    for o in list(bpy.data.objects):
        if o.type != "MESH" or o is cutter: continue
        bpy.context.view_layer.objects.active = o
        mod = o.modifiers.new("__cut__", "BOOLEAN")
        mod.operation = "DIFFERENCE"; mod.object = cutter
        try:
            bpy.ops.object.modifier_apply(modifier=mod.name)
        except Exception:
            try: o.modifiers.remove(mod)
            except Exception: pass
    bpy.data.objects.remove(cutter, do_unlink=True)
    cam.location = (cx, cy - span*2.5, cz)
else:
    sys.exit(2)

direction = mathutils.Vector((cx,cy,cz)) - mathutils.Vector(cam.location)
cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
# Orthographic for the 3 ortho views; perspective for iso + section
if VIEW in ("front", "top", "right"):
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = span * 1.4
bpy.context.scene.camera = cam

# One sun light
light_data = bpy.data.lights.new("sun", type="SUN")
light_data.energy = 4.0
light = bpy.data.objects.new("sun", light_data)
bpy.context.collection.objects.link(light)
light.rotation_euler = (math.radians(45), math.radians(30), 0)

# World: neutral gray
if bpy.context.scene.world is None:
    bpy.context.scene.world = bpy.data.worlds.new("World")
bpy.context.scene.world.use_nodes = True
bg = bpy.context.scene.world.node_tree.nodes.get("Background")
if bg: bg.inputs[0].default_value = (0.18, 0.18, 0.20, 1.0)

s = bpy.context.scene
engines = {e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items}
s.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engines else "BLENDER_EEVEE"
s.render.resolution_x, s.render.resolution_y = W, H
s.render.image_settings.file_format = "PNG"
s.render.filepath = OUT
bpy.ops.render.render(write_still=True)
print(f"[mv] {VIEW} -> {OUT}")
'''


def _render_one_view(stl_or_blend: Path, mode: str, view: str,
                     out_png: Path, res: tuple[int, int]) -> bool:
    """Render one camera view into out_png. Returns True on success."""
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as fh:
        fh.write(_BL_RENDER)
        script = Path(fh.name)
    try:
        env = {**os.environ,
               "MV_MODE": mode, "MV_IN": str(stl_or_blend),
               "MV_OUT": str(out_png), "MV_VIEW": view,
               "MV_RES": f"{res[0]}x{res[1]}"}
        r = subprocess.run(["xvfb-run", "-a", "--server-args=-screen 0 800x600x24",
                            "blender", "-b", "-noaudio", "-P", str(script)],
                           env=env, timeout=90,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return r.returncode == 0 and out_png.exists() and out_png.stat().st_size > 0
    finally:
        try: script.unlink()
        except Exception: pass


def _fc_to_stl(asset: Path, stl_out: Path) -> bool:
    """Convert FC asset (STEP/FCStd/etc.) to STL via freecadcmd."""
    script = '''\
import os, sys, FreeCAD as App, Part, Mesh
inp, out = os.environ["MODEL_IN"], os.environ["MODEL_OUT"]
ext = inp.lower().rsplit(".",1)[-1]
if ext in ("fcstd","fcstd1"):
    d = App.openDocument(inp)
    objs = [o for o in d.Objects if hasattr(o,"Shape") and o.Shape and not o.Shape.isNull()]
    if not objs: sys.exit(1)
    Mesh.export(objs, out)
else:
    sh = Part.read(inp)
    if sh.isNull(): sys.exit(1)
    m = Mesh.Mesh()
    for f in sh.Faces:
        try: m.addFacets(f.tessellate(0.1))
        except Exception: pass
    m.write(out)
print(f"[fc] {inp} -> {out}")
'''
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as fh:
        fh.write(script); sp = Path(fh.name)
    try:
        env = {**os.environ, "MODEL_IN": str(asset), "MODEL_OUT": str(stl_out)}
        r = subprocess.run(["freecadcmd", str(sp)], env=env, timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return r.returncode == 0 and stl_out.exists() and stl_out.stat().st_size > 0
    finally:
        try: sp.unlink()
        except Exception: pass


def _compose_atlas(view_pngs: dict, out_png: Path) -> bool:
    """Compose 4 (or 5/6) view PNGs into a single atlas with labels."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("[mv] PIL not available", file=sys.stderr); return False
    views = list(view_pngs.keys())
    n = len(views)
    if n <= 4:
        cols, rows = 2, 2
    else:
        cols, rows = 3, 2     # 2x3 when we add section_y (5 views)
    pad = 4
    tw, th = _TILE
    label_h = 24
    W = cols * tw + (cols + 1) * pad
    H = rows * (th + label_h) + (rows + 1) * pad
    atlas = Image.new("RGB", (W, H), (28, 28, 32))
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
    except Exception:
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(atlas)
    for i, view in enumerate(views):
        c, r = i % cols, i // cols
        x = pad + c * (tw + pad)
        y = pad + r * (th + label_h + pad)
        png = view_pngs[view]
        if png is not None and Path(png).exists():
            try:
                im = Image.open(png).convert("RGB")
                if im.size != (tw, th):
                    im = im.resize((tw, th), Image.LANCZOS)
                atlas.paste(im, (x, y))
            except Exception as e:
                print(f"[mv] failed loading {png}: {e}", file=sys.stderr)
        # Label below the tile
        draw.rectangle([x, y + th, x + tw, y + th + label_h], fill=(40, 40, 44))
        draw.text((x + 6, y + th + 3), view.upper().replace("_", " "),
                  fill=(230, 230, 235), font=font)
    atlas.save(out_png, "PNG", optimize=True)
    return True


def render_multiview(asset: Path, sidecar_path: Path,
                     out_png: Path | None = None,
                     force: bool = False) -> Path | None:
    """Render the atlas. Returns output path on success, None on failure."""
    _CACHE.mkdir(parents=True, exist_ok=True)
    stem = asset.stem
    out_png = out_png or (_CACHE / f"{stem}.png")
    if out_png.exists() and not force and out_png.stat().st_size > 0:
        return out_png

    sidecar = {}
    if sidecar_path.exists():
        try: sidecar = json.loads(sidecar_path.read_text())
        except Exception: pass
    views = _VIEWS_HOLLOW if _is_hollow(sidecar) else _VIEWS_DEFAULT

    app = sidecar.get("app") or (
        "blender" if asset.suffix.lower() == ".blend" else "freecad")

    # Prepare the input file for Blender: STL for FC, .blend directly for BL
    workdir = Path(tempfile.mkdtemp(prefix="mv_"))
    try:
        if app == "freecad":
            stl = workdir / f"{stem}.stl"
            if not _fc_to_stl(asset, stl):
                print(f"[mv] FC→STL failed for {asset}", file=sys.stderr)
                return None
            mode = "stl"; in_path = stl
        else:
            mode = "blend"; in_path = asset

        view_pngs: dict = {}
        for view in views:
            png = workdir / f"{view}.png"
            if _render_one_view(in_path, mode, view, png, _TILE):
                view_pngs[view] = png
            else:
                view_pngs[view] = None
                print(f"[mv] view {view} failed for {asset}", file=sys.stderr)

        if not any(v is not None for v in view_pngs.values()):
            return None
        if not _compose_atlas(view_pngs, out_png):
            return None
        return out_png
    finally:
        # Clean up workdir (atlas already saved out of it)
        import shutil
        try: shutil.rmtree(workdir)
        except Exception: pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--asset", help="Path to one goal asset (STEP/FCStd/blend)")
    g.add_argument("--jobs-file", help="Path to a parallel_orchestrator jobs JSONL")
    ap.add_argument("--sidecar", help="Path to that asset's meta.json (asset mode)")
    ap.add_argument("--out", help="Atlas PNG output (asset mode; default = cache)")
    ap.add_argument("--force", action="store_true",
                    help="Regenerate even if cached")
    args = ap.parse_args(argv)

    if args.asset:
        asset = Path(args.asset)
        sidecar = Path(args.sidecar) if args.sidecar else asset.with_suffix(".meta.json")
        out = Path(args.out) if args.out else None
        p = render_multiview(asset, sidecar, out, args.force)
        if p is None:
            print(f"[mv] FAILED", file=sys.stderr); return 1
        print(f"[mv] ok -> {p}")
        return 0

    # jobs-file mode
    seen: set[str] = set()
    n_ok = n_skip = n_fail = 0
    with open(args.jobs_file) as fh:
        for line in fh:
            line = line.strip()
            if not line: continue
            j = json.loads(line)
            gp = Path(j["goal_path"])
            sc = gp.with_suffix(".meta.json")
            if not sc.exists():
                n_skip += 1; continue
            try:
                meta = json.loads(sc.read_text())
            except Exception:
                n_skip += 1; continue
            asset = Path(meta.get("asset") or "")
            if not asset.exists() or asset.stem in seen:
                n_skip += 1; continue
            seen.add(asset.stem)
            p = render_multiview(asset, sc, force=args.force)
            if p is not None:
                n_ok += 1
                print(f"[ok ] {asset.stem} -> {p}")
            else:
                n_fail += 1
                print(f"[fail] {asset.stem}", file=sys.stderr)
    print(f"DONE ok={n_ok} skip={n_skip} fail={n_fail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
