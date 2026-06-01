"""Deep-introspect a FreeCAD asset and dump every queryable property.

Goal: discover metadata fields beyond bbox/face_count/volume that we could
exploit in GOAL_METADATA. Run with:

    META_ASSET=/path/to/X.step META_OUT=/tmp/survey_X.json \
        freecadcmd survey_fc_metadata.py

Output: hierarchical JSON capturing Shape, Faces (per surface type), Edges
(per curve type), inertia/principal axes, symmetry hints, and FCStd
structure (bodies, sketches, constraints, etc. when present).
"""
import os
import json
import traceback
from collections import Counter

import FreeCAD as App  # type: ignore
import Part            # type: ignore


def _shape_stats(shape):
    out = {
        "volume":        round(shape.Volume, 3),
        "area":          round(shape.Area, 3),
        "length":        round(shape.Length, 3),
        "is_valid":      shape.isValid(),
        "is_closed":     shape.isClosed() if hasattr(shape, "isClosed") else None,
        "shape_type":    shape.ShapeType,
        "n_solids":      len(shape.Solids),
        "n_shells":      len(shape.Shells),
        "n_faces":       len(shape.Faces),
        "n_edges":       len(shape.Edges),
        "n_vertices":    len(shape.Vertexes),
        "n_compsolids":  len(shape.CompSolids),
        "n_wires":       len(shape.Wires),
    }
    # Center of mass + principal moments
    try:
        com = shape.CenterOfMass
        out["center_of_mass"] = [round(com.x, 3), round(com.y, 3), round(com.z, 3)]
    except Exception: pass
    try:
        pp = shape.PrincipalProperties
        # Returns dict: SymmetryAxis, SymmetryPoint, Moments, RadiusOfGyration
        cleaned = {}
        for k, v in pp.items():
            try:
                if hasattr(v, "x"):
                    cleaned[k] = [round(v.x, 3), round(v.y, 3), round(v.z, 3)]
                elif isinstance(v, (tuple, list)):
                    cleaned[k] = [round(c, 3) for c in v]
                else:
                    cleaned[k] = round(float(v), 3)
            except Exception: cleaned[k] = str(v)
        out["principal_properties"] = cleaned
    except Exception as exc:
        out["principal_properties_err"] = str(exc)
    return out


def _surface_taxonomy(shape):
    """For each face, classify surface type. Reveals whether the model has
    revolutions, splines, ruled surfaces, etc. — info NOT in face_count.
    """
    counts = Counter()
    areas  = {}     # surface_type -> total area
    samples = []    # up to 3 example surface params per type
    for f in shape.Faces:
        s = f.Surface
        cls = type(s).__name__
        counts[cls] += 1
        areas[cls] = areas.get(cls, 0.0) + f.Area
        if cls not in {s["type"] for s in samples} and len(samples) < 12:
            entry = {"type": cls, "area": round(f.Area, 3)}
            # Pull a few interesting parameters per type
            try:
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
                elif cls == "Plane":
                    n = s.Axis
                    entry["normal"] = [round(n.x,3), round(n.y,3), round(n.z,3)]
                elif cls == "BSplineSurface":
                    entry["u_degree"] = s.UDegree
                    entry["v_degree"] = s.VDegree
                    entry["u_knots"]  = len(s.UKnotSequence) if hasattr(s, "UKnotSequence") else None
                elif cls == "SurfaceOfRevolution":
                    a = s.Axis
                    entry["axis"]  = [round(a.x,3), round(a.y,3), round(a.z,3)]
                    entry["loc"]   = [round(s.Location.x,3), round(s.Location.y,3), round(s.Location.z,3)]
                elif cls == "SurfaceOfExtrusion":
                    d = s.Direction
                    entry["direction"] = [round(d.x,3), round(d.y,3), round(d.z,3)]
            except Exception: pass
            samples.append(entry)
    return {"counts": dict(counts),
            "areas":  {k: round(v, 3) for k, v in areas.items()},
            "samples": samples}


def _curve_taxonomy(shape):
    counts = Counter()
    for e in shape.Edges:
        try:
            counts[type(e.Curve).__name__] += 1
        except Exception:
            counts["unknown"] += 1
    return dict(counts)


def _symmetry_hints(shape):
    """Look for axis-aligned symmetry from PrincipalProperties: equal moments
    of inertia along axes suggest rotational/reflectional symmetry.
    """
    try:
        pp = shape.PrincipalProperties
        moments = pp.get("Moments")
        if moments is None: return None
        m = [float(c) for c in moments]
        if max(m) <= 0: return None
        # Normalize
        mx = max(m)
        norm = [c/mx for c in m]
        # If two are ~equal and one differs → rotational sym (cylinder-like)
        # If all ~equal → spherical
        # If all differ → asymmetric block
        pairs_eq = sum(1 for i in range(3) for j in range(i+1,3)
                       if abs(norm[i]-norm[j]) < 0.05)
        return {
            "normalized_moments": [round(c,3) for c in norm],
            "approx_pairs_equal": pairs_eq,  # 3=spherical, 1=rotational, 0=asymmetric
        }
    except Exception:
        return None


def _fcstd_structure(doc):
    """When loaded from .FCStd, surface the object tree: bodies, sketches,
    pad/pocket features, datum planes, constraints — NONE of which we extract today.
    """
    objs = []
    for o in doc.Objects:
        rec = {"name": o.Name, "label": o.Label, "type": o.TypeId}
        # Sketches → number of constraints
        if o.TypeId == "Sketcher::SketchObject":
            try:
                rec["n_constraints"] = len(o.Constraints)
                rec["n_geometry"]    = len(o.Geometry)
                rec["support"]       = str(o.Support)
            except Exception: pass
        # PartDesign features carry length/depth/angle parameters
        if o.TypeId.startswith("PartDesign::"):
            for prop in o.PropertiesList:
                if prop in ("Length", "Length2", "Depth", "Angle", "Radius",
                            "Reversed", "Midplane", "TaperAngle"):
                    try: rec[prop] = getattr(o, prop)
                    except Exception: pass
        # Datum
        if o.TypeId.startswith("PartDesign::Plane") or "Datum" in o.TypeId:
            rec["datum"] = True
        objs.append(rec)
    return {"object_count": len(objs), "objects": objs[:30]}  # cap to keep output small


def main():
    asset = os.environ["META_ASSET"]
    out = os.environ.get("META_OUT", "/tmp/survey.json")
    info = {"asset": asset}
    try:
        ext = asset.lower().rsplit(".", 1)[-1]
        if ext in ("fcstd", "fcstd1"):
            doc = App.openDocument(asset)
            info["fcstd"] = _fcstd_structure(doc)
            # Build a fused shape for geometric stats
            objs = [o for o in doc.Objects
                    if hasattr(o, "Shape") and o.Shape and not o.Shape.isNull()]
            shape = objs[0].Shape if objs else None
            for o in objs[1:]:
                if not o.Shape.isNull(): shape = shape.fuse(o.Shape)
        else:
            shape = Part.read(asset)
        if shape is not None and not shape.isNull():
            info["shape_stats"]      = _shape_stats(shape)
            info["surface_taxonomy"] = _surface_taxonomy(shape)
            info["curve_taxonomy"]   = _curve_taxonomy(shape)
            info["symmetry_hints"]   = _symmetry_hints(shape)
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        info["trace"] = traceback.format_exc()
    with open(out, "w") as fh:
        json.dump(info, fh, indent=2, default=str)
    print(f"[survey-fc] -> {out}")


if __name__ == "__main__":
    main()
