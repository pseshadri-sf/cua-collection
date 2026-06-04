# compositional_dynamics — per-component build replay (training videos)

Goal: keep the planner's end-asset quality, but decompose the one-shot full_code
into a SEQUENCE of code actions (one per elementary component) so the video shows
the asset built in fine gradation — clean training data.

## How it works
- Planner `--compositional`: each `steps[].code` is rewritten to be an
  individually runnable + immediately-visible statement — builds ONE component,
  positions it, addObject as its own named object, doc.recompute() so it appears.
  Step 1 inits the doc (import + ActiveDocument-or-new + clear); later steps reuse
  the persistent console namespace. Components stay SEPARATE (not fused).
- Runner `--compositional`: deterministically replays the plan's per-component
  steps, one python_eval each, capturing step_NN_after.png after each, then
  terminates. Same end asset as one-shot; trajectory = N component actions.

## Smoke (adirondack chair)
12-component plan -> trajectory of 12 distinct python_evals (labeled
"component k/12: <name> — <why>") + terminate, 12 progression frames,
term=agent, score 58.0. Video shows: panel -> frame -> full chair, one
component at a time. FreeCAD tree grows part_00..part_11.

## Status / next
- FreeCAD path implemented + verified. Blender compositional prompt added
  (_COMPOSITIONAL_BL) but BL runner replay not yet wired (FC runner only).
- To generate a training set: run with --planner-model gemini-3.1-pro
  --compositional over the asset list (deterministic; ~planner cost + short
  trajectories). Each job yields a fine-grained build video + per-action labels.
