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
