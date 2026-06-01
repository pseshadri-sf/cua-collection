# TODO

## Auto-enable `--decompose` when object_count > 1

**Status:** deferred (Wave-4.1 follow-up — `tool-calling` branch, commit `dcf0de3`)

**Context:** Wave-4.1 showed that injecting a per-part bbox+origin table into
`GOAL_METADATA` lifts multi-part match_score from 52.4 → 62.8 on a 10-asset
multi-part subset (+10.4 mean, face_ratio +0.15, vert_ratio +0.16). The
`--decompose` flag is currently opt-in — it's not default-on because it
requires the sidecar to carry a `parts` array, which only happens when
`build_goal_metadata_sidecars.py --decompose` was run.

**Change:** flip `--decompose` on by default in the orchestrator pre-flight
when `object_count > 1` is detected on the sidecar. Sketch:

1. `scripts/parallel_orchestrator.py::ensure_metadata_sidecars`: after the
   first-pass extraction, re-load each sidecar; if `object_count > 1` and
   the sidecar lacks a `parts` array, invoke the decomposer
   (`decompose_freecad_asset.py` / `decompose_blender_asset.py`) and merge
   the manifest in.
2. Same orchestrator: append `--decompose` to each job's `extra_args` when
   the sidecar carries a `parts` array.

**Why opt-in for now:** the regression on the dense-scatter 100-part scene
(W4 89 → W4.1 51) shows a failure mode — when the goal has too many tiny
parts, the decomposed sidecar's truncated 12/100 list scores worse than
W4's single bbox-correct primitive that happened to volume-match. Before
flipping default-on, decide:

- Should we skip decomposition above a `parts_truncated` ratio threshold
  (e.g. if we keep < 25% of parts, use single-primitive mode)?
- Or: redesign the prompt to say "the asset has 100 small parts; build a
  bounding shell and don't try to enumerate" — i.e. a third "swarm" prompt
  variant alongside primitive / decomposed.

**Verification command after shipping:**

```bash
bash ~/cua_gui_smoketest/run_parallel_trajectories.sh \
    --num-workers 4 --jobs-file <multi-part jobs file> \
    --output-dir <run dir>
# Inspect: <run-dir>/<job>/eval/eval.json — should show
# part-by-part build calls in the trajectory.
```

## Open intervention: post-build feedback signal

Wave-4 / 4.1 traces consistently end with `agent_loop_detected` after the
agent emits 3 identical `python_eval` payloads. The first call commits and
produces geometry, but the agent can't see it (off-frame in the viewport)
and re-types the same code thinking it failed.

**Sketch:** after every `python_eval`, inject `LAST_BUILD_SUCCEEDED: True,
objects_in_scene: [...]` into the next turn's user message — extracted from
the FreeCAD doc / bpy scene via the same mechanism that captures
screenshots. Removes the need for the agent to visually verify
geometry was created.


## BL decomposer: multi-strategy dispatch for real-world assets

**Status:** deferred (Wave-6 follow-up — current procedurally-generated test
set doesn't trigger any of these branches, all assets are flat sibling
top-level objects with no hierarchy/collections/instancing/etc.)

**Context:** the current `decompose_blender_asset.py` uses one strategy —
enumerate `bpy.data.objects`, one part per object. Probing the W6 test
assets confirms this is correct for *those* assets (zero parenting, zero
collections, zero vertex_groups, zero multi-material faces, zero linked data,
zero multi-island meshes across all 7 representative samples). But .blend
carries 8+ axes of structural metadata, and any real-world asset
(GLB/FBX/Sketchfab download, rigged character, kitbash scene) would use them.

**Change:** add a `decomposition_strategy` field to the BL extractor; pick
the first applicable strategy in priority order:

1. **Armature present** → bone-hierarchy decomposition + per-bone vertex
   weights (rigged characters). Read `bpy.data.armatures`,
   `obj.parent_type == "BONE"`.
2. **Parent tree non-trivial** (`any(o.parent for o in bpy.data.objects)`)
   → traverse the scene-graph tree, emit a hierarchical parts list so the
   agent knows shoulder→arm→hand→finger structure.
3. **Collections non-empty** (`bpy.data.collections`) → group parts by
   collection. Author intent: "body parts", "fasteners", "trim".
4. **Linked instances** (`obj.library is not None` OR `mesh.users > 1` on
   multiple objects) → emit unique-mesh list + per-instance transform
   list, so the agent can build one + transform-paste N times (ARRAY
   modifier or instance loop).
5. **Multi-island mesh** (connected-component count > 1 on a single MESH
   object) → split via flood-fill on edge graph; classic "joined mesh"
   case where N floating bodies sit inside one object.
6. **Multi-material mesh** (`len(material_slots) > 1` and
   `polygons.material_index` not all the same) → group faces by
   `material_index`, emit per-slot sub-geometry.
7. **Default** → current flat `bpy.data.objects` enumeration.

Each strategy needs a matching prompt template block in `vlm_client.py`.
Cost ~10 LOC per strategy in the decomposer + ~30 LOC per template block.
None of this is on the critical path for the current procedural test set —
ship only when real-world assets enter the pipeline.

**Custom properties pickup:** orthogonal to the strategy ladder, capture
`o.keys()` (custom props like `"part_kind": "leg"`, `"manufacturer": "ACME"`)
into the manifest. Production pipelines stash semantic info there.

**Verification idea:** once shipped, download 5 rigged characters from
BlenderKit and 5 industrial CAD imports from GrabCAD, run them through the
extractor, confirm each picks a non-default strategy.


## FC decomposer: BSpline / SurfaceOfRevolution recipe template

**Status:** deferred (Wave-6 — clearest single-asset-class win remaining for FC)

**Context:** Wave-6 FC mean was 54.1, with the cluster of hardest assets
(sprockets, faucets, hinges, cans) all stuck at 47–58 because the agent
falls back to `Part.makeBox` of the right size when the surface taxonomy
contains `BSplineSurface` or `SurfaceOfRevolution`. The hex-prism worked
example (Wave-5.1) was the proof-of-concept: a single revolved-shape recipe
in the prompt lifted standoff from 66 → 80 with no other change.

**Sketch:** when sidecar's `surface_taxonomy.counts` contains
`BSplineSurface`, `SurfaceOfRevolution`, or `Cone` alongside multiple Lines
and Circles in the curve_taxonomy, append a `_W7_REVOLUTION_RECIPE_BLOCK`
template to GOAL_METADATA with worked examples for:

  - Single-axis revolution: `Part.makeRevolution(profile_wire, axis, angle)`
  - Loft between two profiles: `Part.makeLoft([wire1, wire2])`
  - Sweep along a path: `Part.makeSweep(wire, path)`
  - Cup/can shape: cylinder + cylinder cut + bottom plane

The revolution profile can be approximated from the goal screenshot's
silhouette — encode a 4–6 point profile from "obvious" features.

**Expected lift:** standoff was +14 from one recipe. Hinge (51.9), faucets
(50.5), cans (55.2-56.2), cup (48.4), sprockets (~48 avg) — if the recipe
template lifts each by similar amounts, FC mean could close from 54 → 65+
on the W6 asset set with no model change.
