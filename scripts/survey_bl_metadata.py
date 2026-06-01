"""Deep-introspect a Blender .blend asset and dump every queryable property.

Goal: discover metadata fields beyond bbox/face_count that we could exploit
in GOAL_METADATA. Run with:

    META_ASSET=/path/to/X.blend META_OUT=/tmp/survey_X.json \
        blender -b -P survey_bl_metadata.py

Output captures: per-object hierarchy, modifier stacks, materials, animation,
curve/surface/text data blocks, particles, constraints — most of which the
current sidecar ignores.
"""
import os
import sys
import json
import traceback

import bpy           # type: ignore
import mathutils     # type: ignore


def _vec3(v) -> list:
    return [round(v[0], 4), round(v[1], 4), round(v[2], 4)]


def _modifier_summary(obj) -> list:
    out = []
    for m in obj.modifiers:
        entry = {"name": m.name, "type": m.type}
        # Surface the most consequential parameters per type
        if m.type == "ARRAY":
            entry["count"]              = m.count
            entry["fit_type"]           = m.fit_type
            entry["relative_offset"]    = _vec3(m.relative_offset_displace)
            entry["constant_offset"]    = _vec3(m.constant_offset_displace)
            entry["use_object_offset"]  = m.use_object_offset
        elif m.type == "MIRROR":
            entry["use_axis"]      = list(m.use_axis)
            entry["use_bisect"]    = list(m.use_bisect_axis)
            entry["mirror_object"] = m.mirror_object.name if m.mirror_object else None
        elif m.type == "BOOLEAN":
            entry["operation"] = m.operation
            entry["operand"]   = (m.object.name if m.object else None)
            entry["solver"]    = m.solver
        elif m.type == "BEVEL":
            entry["width"]      = round(m.width, 4)
            entry["segments"]   = m.segments
            entry["limit_method"] = m.limit_method
            entry["affect"]     = m.affect
        elif m.type == "SUBSURF":
            entry["levels"]     = m.levels
            entry["render_levels"] = m.render_levels
            entry["subdivision_type"] = m.subdivision_type
        elif m.type == "SOLIDIFY":
            entry["thickness"]  = round(m.thickness, 4)
            entry["offset"]     = round(m.offset, 4)
        elif m.type == "SCREW":
            entry["screw_offset"] = round(m.screw_offset, 4)
            entry["angle"]      = round(m.angle, 4)
            entry["steps"]      = m.steps
            entry["axis"]       = m.axis
        elif m.type == "DISPLACE":
            entry["strength"]   = round(m.strength, 4)
            entry["midlevel"]   = round(m.mid_level, 4)
            entry["texture"]    = (m.texture.name if m.texture else None)
        elif m.type == "WAVE":
            entry["height"]     = round(m.height, 4)
            entry["width"]      = round(m.width, 4)
        out.append(entry)
    return out


def _material_summary(obj) -> list:
    out = []
    for slot in obj.material_slots:
        m = slot.material
        if m is None:
            out.append({"empty": True}); continue
        entry = {"name": m.name, "use_nodes": m.use_nodes}
        if m.use_nodes and m.node_tree:
            entry["node_types"] = sorted({n.type for n in m.node_tree.nodes})
            entry["n_nodes"]    = len(m.node_tree.nodes)
            # Find a Principled BSDF and pull base color
            for n in m.node_tree.nodes:
                if n.type == "BSDF_PRINCIPLED":
                    try:
                        bc = n.inputs.get("Base Color")
                        if bc is not None:
                            entry["base_color"] = [round(c, 3) for c in bc.default_value]
                    except Exception: pass
                    try:
                        rg = n.inputs.get("Roughness")
                        if rg is not None:
                            entry["roughness"] = round(rg.default_value, 3)
                    except Exception: pass
                    break
        out.append(entry)
    return out


def _vertex_groups(obj) -> list:
    return [g.name for g in (obj.vertex_groups or [])][:20]


def _animation(obj) -> dict | None:
    ad = obj.animation_data
    if ad is None or ad.action is None: return None
    a = ad.action
    fcurves = []
    for fc in a.fcurves[:20]:
        fcurves.append({"data_path": fc.data_path, "n_keyframes": len(fc.keyframe_points)})
    return {"action": a.name, "frame_range": list(a.frame_range), "fcurves": fcurves}


def _curve_data(data) -> dict:
    out = {"dimensions": data.dimensions, "splines": []}
    for sp in data.splines[:10]:
        out["splines"].append({
            "type": sp.type,
            "n_points": len(sp.points) if sp.type == "POLY"
                        else (len(sp.bezier_points) if sp.type == "BEZIER"
                              else len(sp.points)),
            "use_cyclic": sp.use_cyclic_u,
            "order_u": sp.order_u,
        })
    return out


def _text_data(data) -> dict:
    return {"body":   (data.body or "")[:64],
            "extrude": round(data.extrude, 4),
            "size":    round(data.size, 4),
            "font":    (data.font.name if data.font else None)}


def _object_summary(obj) -> dict:
    entry = {
        "name":      obj.name,
        "type":      obj.type,
        "location":  _vec3(obj.location),
        "rotation":  _vec3(obj.rotation_euler),
        "scale":     _vec3(obj.scale),
        "dimensions": _vec3(obj.dimensions),
        "parent":    (obj.parent.name if obj.parent else None),
        "n_constraints": len(obj.constraints),
        "constraint_types": [c.type for c in obj.constraints[:10]],
        "n_children": len(obj.children),
    }
    if obj.modifiers:
        entry["modifiers"] = _modifier_summary(obj)
    if obj.material_slots:
        entry["materials"] = _material_summary(obj)
    if obj.vertex_groups:
        entry["vertex_groups"] = _vertex_groups(obj)
    ad = _animation(obj)
    if ad: entry["animation"] = ad

    if obj.type == "MESH" and obj.data:
        m = obj.data
        entry["mesh"] = {
            "vertex_count":   len(m.vertices),
            "edge_count":     len(m.edges),
            "polygon_count":  len(m.polygons),
            "n_uv_layers":    len(m.uv_layers),
            "n_color_attrs":  len(getattr(m, "color_attributes", []) or []),
            "n_shape_keys":   (len(m.shape_keys.key_blocks) if m.shape_keys else 0),
            "has_custom_normals": getattr(m, "has_custom_normals", None),
            "vertex_attrs":   sorted({a.name for a in getattr(m, "attributes", [])})[:20],
        }
    elif obj.type == "CURVE" and obj.data:
        entry["curve"] = _curve_data(obj.data)
    elif obj.type == "FONT" and obj.data:
        entry["text"] = _text_data(obj.data)
    elif obj.type == "SURFACE":
        entry["surface"] = {"splines": len(obj.data.splines) if obj.data else 0}
    elif obj.type == "META":
        entry["metaball"] = {"elements": len(obj.data.elements) if obj.data else 0}
    elif obj.type == "ARMATURE":
        entry["armature"] = {"bones": len(obj.data.bones) if obj.data else 0}
    elif obj.type == "EMPTY":
        entry["empty_display_type"] = obj.empty_display_type
    return entry


def main():
    asset = os.environ["META_ASSET"]
    out   = os.environ.get("META_OUT", "/tmp/survey.json")
    info = {"asset": asset}
    try:
        bpy.ops.wm.open_mainfile(filepath=asset)
        scene = bpy.context.scene
        info["scene"] = {
            "name":         scene.name,
            "frame_start":  scene.frame_start,
            "frame_end":    scene.frame_end,
            "render_engine": scene.render.engine,
            "fps":          scene.render.fps,
            "world":        (scene.world.name if scene.world else None),
            "active_camera": (scene.camera.name if scene.camera else None),
            "n_objects":    len(bpy.data.objects),
            "n_meshes":     len(bpy.data.meshes),
            "n_materials":  len(bpy.data.materials),
            "n_curves":     len(bpy.data.curves),
            "n_lights":     len(bpy.data.lights),
            "n_cameras":    len(bpy.data.cameras),
            "n_collections": len(bpy.data.collections),
            "n_textures":   len(bpy.data.textures),
            "n_images":     len(bpy.data.images),
            "n_particle_systems": len(bpy.data.particles),
            "n_node_groups":      len(bpy.data.node_groups),
        }
        info["collections"] = [
            {"name": c.name, "n_objects": len(c.objects), "n_children": len(c.children)}
            for c in bpy.data.collections[:10]
        ]
        info["objects"] = [_object_summary(o) for o in bpy.data.objects[:30]]
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        info["trace"] = traceback.format_exc()
    with open(out, "w") as fh:
        json.dump(info, fh, indent=2, default=str)
    print(f"[survey-bl] -> {out}")


if __name__ == "__main__":
    main()
