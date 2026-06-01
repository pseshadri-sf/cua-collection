# Asset metadata survey: what we can exploit to improve the agent

**Date:** 2026-06-01 · **Branch:** `tool-calling` · **Scope:** 10 representative
FC and BL assets covering primitives, compounds, revolutions, text,
modifier stacks, dense scatter.

This survey enumerates metadata fields available in the source CAD assets
that we **don't currently expose** to the agent through `GOAL_METADATA`.
Every field listed is queryable with a few lines of Python in
`freecadcmd` / `blender -b`, so the extraction cost is "one more line in
`extract_goal_metadata.py`" — not a research project.

Discovery scripts (kept for future re-runs):

- `scripts/survey_fc_metadata.py` — hands-on FC introspection
- `scripts/survey_bl_metadata.py` — hands-on BL introspection
- Output: `/tmp/survey/{fc,bl,mod}_<asset>.json`

---

## What `GOAL_METADATA` already carries (Wave-4 / Wave-4.1)

| Field                      | Source                        | Used? |
|----------------------------|-------------------------------|-------|
| `bbox_mm` / `bbox_normalized` | `shape.BoundBox` / bpy bbox | ✅    |
| `object_count`             | solid count / bpy.data.objects | ✅    |
| `face_count`, `vertex_count`, `edge_count` | shape | ✅ |
| `volume_mm3`               | `shape.Volume`                | ✅ |
| `dominant_primitive_class` | brittle heuristic on face mix | ✅ (often wrong) |
| `parts[]`: per-part bbox + origin | decomposer manifests   | ✅ (Wave-4.1) |

That's it. Below is the gold we're leaving on the floor.

---

## High-value fields we should exploit (ranked)

### 🟢 TIER 1 — high impact, ~5 min extraction work each

#### **F1. FC surface-type histogram + per-face parameters** (`surface_taxonomy`)

Every `shape.Faces[i].Surface` is a typed Geom object: `Plane`, `Cylinder`,
`Cone`, `Sphere`, `Toroid`, `BSplineSurface`, `SurfaceOfRevolution`,
`SurfaceOfExtrusion`. Each carries the construction parameters used to
generate it.

Example — hex standoff (currently misclassified as `revolution`):

```json
"surface_taxonomy": {
  "counts": {"Plane": 9, "Cylinder": 2, "Cone": 2},
  "samples": [
    {"type": "Cylinder", "radius": 1.5, "axis": [0,0,1]},
    {"type": "Cone",     "semi_angle": 0.7854, "radius": 1.5},
    {"type": "Plane",    "normal": [0,0,1]}
  ]
}
```

→ Agent can immediately infer "9-plane prism (hex) extruded along Z, with a
ø1.5 cylinder through-axis and 2 cone chamfers." Replaces guessing with
exact construction info.

**Why it matters:** the W4 standoff scored 66 with the agent emitting a
plain `Part.makeBox(5.5, 6.3, 21)` because the prompt only said "revolution".
With surface taxonomy, the agent knows it's a hex prism + bore and would
emit `Part.makePolygon` / `Part.Extrude` + `Part.makeCylinder.cut()`.

**Extraction cost:** 15 lines in `extract_goal_metadata.py` (already
prototyped in `survey_fc_metadata.py::_surface_taxonomy`).

#### **F2. FC edge-curve histogram** (`curve_taxonomy`)

Each `shape.Edges[i].Curve` is `Line`, `Circle`, `Ellipse`, `BSplineCurve`,
`BezierCurve`, `Hyperbola`. The histogram tells the agent whether the goal
has straight edges only (extruded prism), circular edges (revolutions /
cylinders), or splines (organic / NURBS surfaces). Adirondack chair: 507
Lines + 108 Circles → cylindrical legs joined by rectilinear slats.

Cost: 3 lines.

#### **B1. BL per-object rotation + parent hierarchy**

Currently the decomposer dumps `location` and `dimensions` but **not
`rotation_euler` or `parent`**. The kitbash robot's arms have
`rotation = (0, 1.5708, 0)` (90° about Y) — without this the agent builds
vertical cylinders instead of horizontal arms.

```
Cylinder      loc=(-1.3, 0, 1.0)  rot=(0, 1.5708, 0)  dims=(0.4, 0.4, 1.6)
Cylinder.001  loc=( 1.3, 0, 1.0)  rot=(0, 1.5708, 0)  dims=(0.4, 0.4, 1.6)
```

Cost: 2 lines added to the decomposer manifest + 1 line in the prompt template.

#### **B2. BL object.type discrimination — FONT / CURVE / META / SURFACE**

Right now the sidecar squashes everything into "MESH". The
`text_multi_line` asset has TWO `FONT` objects:

```
Text     type=FONT  body="HELLO"  size=1.0  extrude=0.15  font=Bfont  loc=(-1.5, 1, 0)
Text.001 type=FONT  body="WORLD"  size=1.0  extrude=0.15  font=Bfont  loc=(-1.5,-1, 0)
```

That asset scored **5/100** in Wave-4 because the agent had no way to know
to emit `bpy.ops.object.text_add()` with `body="HELLO"`. With these 4
fields injected, the agent gets to 95+ trivially.

The same logic applies to `CURVE` objects (Bezier/NURBS splines) and
`META` (metaballs) — both common in the test set.

Cost: 8 lines (per-type extractor — already in `_object_summary` of the
survey script).

#### **B3. BL modifier stack with parameters**

The test set has 9+ modifier-stack assets:
`13_torus_array` (ARRAY count=5), `17_screw_spring` (SCREW offset=4.0
angle=2π steps=16 axis=Z), `28_bool_union_chain` (chained BOOLEAN UNIONs),
`30_subdiv_bevel_cube` (BEVEL width=0.25 + SUBSURF levels=3).

Without modifier metadata the agent must reverse-engineer "this looks like
an array of 5 toruses" from a screenshot — exactly the kind of inference
where Wave-1/2/3 prompt interventions failed. With it, the agent can emit:

```python
m = obj.modifiers.new('a', 'ARRAY'); m.count = 5
m.relative_offset_displace = (1.5, 0, 0)
```

…and score 90+ instead of treating it as 5 separate toruses with messy
volumes.

Cost: 20 lines (one helper per modifier type — already prototyped in
`survey_bl_metadata.py::_modifier_summary`).

---

### 🟡 TIER 2 — medium impact, ~15 min work each

#### **F3. CenterOfMass + PrincipalProperties → symmetry detection**

For STEP solids that expose `PrincipalProperties` (older FreeCAD doesn't —
fallback gracefully): equal principal moments along two axes flag
rotational symmetry; all-three-equal flags spherical symmetry. Plus
`CenterOfMass` reveals where the bulk of the geometry actually sits
(useful when the bbox is much larger than the dense volume — e.g. a
chair where 80% of the volume is in the seat).

Cost: 10 lines + graceful try/except for FC ≤ 0.19.

#### **F4. FCStd PartDesign feature graph (when source is .FCStd)**

For the 24 `.FCStd` assets in the FC library, each `PartDesign::Pad` /
`Pocket` / `Hole` / `Fillet` carries its **exact construction parameters**:

```
Type=PartDesign::Pad     Length=20.0     Reversed=False  Midplane=True
Type=PartDesign::Pocket  Length=5.0      Depth=10
Type=PartDesign::Fillet  Radius=2.0
```

These are the literal numbers the original author typed into the
parametric model. A `--grounded --decompose --parametric` flag would
feed them straight to the agent. STEP files don't have this (it's lost in
neutral-format export) — so this only helps for the FCStd corner — but
when it applies it's near-perfect signal.

Cost: 15 lines.

#### **B4. Materials / base colors**

```
Cube  → materials=[{name:"Red", base_color:[0.9,0.1,0.1,1], roughness:0.4}]
```

Doesn't help the geometric evaluator (the current reward function ignores
color) but would help if we ever start scoring visual fidelity. Defer
until we add a color-aware reward.

Cost: 5 lines, deferred.

---

### 🔵 TIER 3 — low impact / niche, deferred

| Field | Why deferred |
|-------|--------------|
| BL animation / NLA strips | Goal screenshots are static; no signal |
| BL armature / bones | No skeletal assets in current test set |
| BL particle systems | None in current assets |
| BL vertex groups / shape keys | Reconstruction is "what shape," not "what rig" |
| FC `Wires` / `CompSolids` | Subsumed by surface + curve taxonomy |
| BL `n_uv_layers`, `n_color_attrs` | Texture/UV reconstruction is out of scope |

---

## Cross-cutting opportunities (post-Wave-4.1)

### **X1. Symmetry hint → MIRROR modifier suggestion**

For BL: when `surveyed_objects` contain a `MIRROR` modifier, we already
have it (Tier 1 B3). For FC and BL-without-modifiers: detect equal-spaced
parts (e.g. `table_4_legs` has 4 identical cylinders with `(±0.85, ±0.45,
0)` origins) and tell the agent "mirror about XY". Reduces the per-part
recipe from 4 builds → 1 build + 1 mirror.

Cost: a 20-line `detect_symmetry(parts)` postprocessor on the decomposer
manifest.

### **X2. Repetition detection → ARRAY modifier suggestion**

When the decomposed parts list contains N parts with identical bbox (the
`b42_dense_scatter_100` case — 100 cubes), instead of listing them
individually (truncated to 12) tell the agent: "100 cubes of size 0.18,
randomly distributed in (±2, ±2, 0..2) — use a `random.seed(42); for _ in
range(100): primitive_cube_add(location=...)` loop."

This directly addresses the Wave-4.1 regression on dense scatter
(89 → 51 because we truncated 100 → 12).

Cost: 30 lines + a new prompt branch in the metadata template.

### **X3. Bound-axis-aligned-extrusion detection**

Most STEP files of the form "single solid, all planes parallel to axes,
one curved surface" are simple Pad+Pocket extrusions. Detect this and
emit the cross-section + extrude direction instead of the bounding box.

E.g. the H-beam profile (currently scores 57) is literally an H-shaped
cross-section extruded along Z. With the H-cross-section sketch encoded
as 4 boxes + fuse, this becomes a 100-score.

Cost: 50 lines (cross-section extraction is real geometric work).

---

## Proposed roadmap

**Sprint 1 (this week's follow-up):** F1, F2, B1, B2, B3 — all Tier-1,
all <100 lines combined. Should give another +5–10 mean on the
hard-asset set, with particularly large wins on:

- Text assets (5 → 95): B2 alone
- Modifier-stack assets (current ~50 → likely 90+): B3
- Kitbash / multi-rotation assets (~85 → ~95): B1
- Misclassified primitives (66 → ~95): F1 + F2

**Sprint 2:** F3 (symmetry hint), X1 (symmetry → mirror), X2 (repetition →
array). These compound nicely with Sprint 1 — once the agent knows the
exact per-part info AND the symmetry structure, the output should be
near-pixel-perfect on regular assemblies.

**Sprint 3:** F4 (FCStd parametric features). Niche but free wins on the
24 FCStd assets.

**Defer:** Tier 3 — none of it is on the critical path until we
add multi-modal reward components (color, animation, etc.).

---

## Implementation note

The extraction work is already mostly done in `survey_fc_metadata.py` and
`survey_bl_metadata.py`. Promoting these to sidecar fields is a matter of
copying the helpers into `extract_goal_metadata.py`, adding template
slots in `vlm_client.py::_W4_GROUNDED_HEADER`, and bumping the metadata
version field on the sidecar.

Backwards compatibility: existing sidecars don't carry the new fields, so
`_render_grounded_metadata` should treat each new field as optional and
skip its template block when absent.
