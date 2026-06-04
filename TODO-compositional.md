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

## Wave-14 scale eval (50 assets) — quality does NOT hold at scale
| | compositional | one-shot (wave-11) | Δ |
|---|---|---|---|
| FreeCAD (25) | 63.2 | 75.1 | -11.9 |
| Blender (25) | 85.9 | 94.1 | -8.2 |
Cost: $0.0225/asset (PLANNER ONLY — deterministic replay makes 0 SLM calls),
60s/asset, ~0.8h total. Ultra-cheap + fast, clean per-component videos.

Score drop has 3 distinct causes (NOT a single regression):
1. comps=0 planner failures: compositional plan sometimes has no per-step
   `code` -> deterministic replay builds NOTHING -> ~0 (torus_array 90->33,
   nested_spheres 97->48). Planner-reliability bug in compositional mode.
2. single-solid assets forced to comps=1: a complex single solid (screw, bend)
   built as ONE coarse component loses the one-shot's richer code
   (fastener 86->55, rect_bend 78->53).
3. boolean assets: the "do NOT fuse/compound" directive breaks parts that need
   booleans (bool_cube_sphere 95->53). Only 4/50 plans used booleans.
Parity holds ONLY for additive multi-part assets (torus/monkey/sphere_ring/smd
matched one-shot exactly) — i.e. the chair/robot smoke type.

## Fix to recover quality (keep compositional dynamics)
1. Fall back to one-shot full_code when object_count<=1 / comps<=1 (compositional
   only applies to multi-component assets).
2. Allow per-component booleans (a component MAY be a boolean of primitives,
   added as one visible object); only forbid fusing ACROSS components.
3. Guard comps=0: if no per-step code, split full_code into statements or re-plan.

## Two-stage decomposition — final results (wave-16/17)
Stage1 = good full_code; Stage2 = LLM decomposes that build into visible steps.
| | compositional | one-shot | Δ | multi-step |
|---|---|---|---|---|
| Blender | 93.2 | 94.1 | -0.9 (PARITY) | 25/25 |
| FreeCAD (stage1 low)  | 64.5 | 75.1 | -10.6 | 20/25 |
| FreeCAD (stage1 med)  | 65.5 | 75.1 | -9.6  | 21/25 |
Cost $0.043/asset ($2.16/50), zero SLM.

- BL: SOLVED — two-stage compositional at parity, all assets decompose, clean
  fine-grained build videos. Robust framing + Start-page-to-front + force-visible
  make every step visible.
- FC: residual ~10pt loss. Stage-2 cannot faithfully reproduce FC's
  boolean/compound/fillet (CSG) builds when split into per-step objects/features;
  topology reorganizes -> fidelity loss. Single-solid feature sequences work
  case-by-case (screw 82.7) but not uniformly. Stage-1 reasoning is NOT the
  bottleneck (medium didn't help at scale).
- Option for guaranteed FC parity: append the original full_code as a final
  "snap" step so the END geometry == one-shot (video shows the process, final
  frame is exact). Tradeoff: a clear+rebuild flicker at the end of the video.
