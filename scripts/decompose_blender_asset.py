"""Decompose a Blender .blend asset into per-object sub-assets.

Run with blender headless:
    BLDEC_ASSET=/path/to/scene.blend BLDEC_OUT=/path/to/out_dir \
        blender -b -P decompose_blender_asset.py

Outputs:
    out_dir/
        manifest.json
        part_00.blend   (scene containing only object 0 + camera + light)
        part_01.blend
        ...

manifest.json schema:
    {
      "asset":      "/orig/path",
      "app":        "blender",
      "kind":       "multi-object" | "single-object" | "modifier-stack",
      "part_count": N,
      "parts": [
        {"index": 0, "name": "Cube.001", "path": "part_00.blend",
         "location": [x,y,z], "rotation_euler": [rx,ry,rz], "scale": [sx,sy,sz],
         "dimensions": [w,h,d], "vertex_count": N, "face_count": M}
      ]
    }
"""
import os
import sys
import json
import shutil

import bpy  # type: ignore[import-not-found]


def _isolate_and_save(target_name: str, out_blend: str, source_blend: str) -> None:
    """Re-open the source blend, delete every mesh object EXCEPT target_name,
    keep the camera + light, then save_as_mainfile to out_blend."""
    bpy.ops.wm.open_mainfile(filepath=source_blend)
    to_delete = []
    for obj in bpy.data.objects:
        if obj.type == "MESH" and obj.name != target_name:
            to_delete.append(obj)
    bpy.ops.object.select_all(action="DESELECT")
    for obj in to_delete:
        obj.select_set(True)
    if to_delete:
        bpy.ops.object.delete()
    # If the surviving object isn't at origin, optionally re-centre — keep
    # original location for now (stitcher needs to know where parts belong).
    bpy.ops.wm.save_as_mainfile(filepath=out_blend, check_existing=False)


def _stats(obj) -> dict:
    mesh = obj.data
    return {
        "vertex_count": len(mesh.vertices),
        "face_count":   len(mesh.polygons),
        "edge_count":   len(mesh.edges),
    }


def main() -> int:
    asset = os.environ.get("BLDEC_ASSET")
    out_dir = os.environ.get("BLDEC_OUT")
    if not asset or not out_dir:
        print("ERROR: set BLDEC_ASSET and BLDEC_OUT env vars", file=sys.stderr)
        return 2
    if not os.path.exists(asset):
        print(f"ERROR: asset not found: {asset}", file=sys.stderr)
        return 2
    os.makedirs(out_dir, exist_ok=True)

    # Load once to enumerate mesh objects + grab metadata.
    bpy.ops.wm.open_mainfile(filepath=asset)
    mesh_objects = [o for o in bpy.data.objects if o.type == "MESH"]
    if not mesh_objects:
        # Maybe metaballs / text / curves — fall back to all non-camera/light
        mesh_objects = [o for o in bpy.data.objects
                        if o.type in {"META", "FONT", "CURVE", "SURFACE"}]
    has_modifiers = any(len(o.modifiers) > 0 for o in mesh_objects)

    if len(mesh_objects) == 1 and has_modifiers:
        kind = "modifier-stack"
    elif len(mesh_objects) == 1:
        kind = "single-object"
    else:
        kind = "multi-object"

    # Capture metadata BEFORE we start mutating scenes during isolation.
    captured = []
    for i, obj in enumerate(mesh_objects):
        captured.append({
            "index": i,
            "name": obj.name,
            "type": obj.type,
            "location":        [round(float(v), 4) for v in obj.location],
            "rotation_euler":  [round(float(v), 4) for v in obj.rotation_euler],
            "scale":           [round(float(v), 4) for v in obj.scale],
            "dimensions":      [round(float(v), 4) for v in obj.dimensions],
            "modifier_count":  len(obj.modifiers),
            "modifier_types":  [m.type for m in obj.modifiers],
            **(_stats(obj) if obj.type == "MESH" else {"vertex_count": 0,
                                                      "face_count": 0,
                                                      "edge_count": 0}),
        })

    # Now write per-part .blend files (one re-open per part — slow but robust).
    parts = []
    for meta in captured:
        out_blend = os.path.join(out_dir, f"part_{meta['index']:02d}.blend")
        try:
            _isolate_and_save(meta["name"], out_blend, asset)
            parts.append({**meta, "path": os.path.basename(out_blend)})
        except Exception as exc:
            print(f"[err] part {meta['index']} ({meta['name']}): {exc}",
                  file=sys.stderr)

    manifest = {
        "asset": asset,
        "app": "blender",
        "kind": kind,
        "part_count": len(parts),
        "parts": parts,
    }
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"[ok] {asset} → {manifest['part_count']} parts ({kind})")
    print(f"      manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
