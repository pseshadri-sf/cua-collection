# Wave-9d — Comprehensive Failure-Mode Analysis

Benchmark run: `runs/wave9d_20260602T160657Z` (code = `wave-8.1`, i.e. Wave-8
three-fix set for FC + BL synthetic-injection reverted).

**50 NEW difficult assets**, distinct from wave6/wave8 at the canonical-asset
level (deduped across screenshot captures). Split **32 FreeCAD / 18 Blender**
(BL library is nearly exhausted — only 35 unused assets remain). FC spans 26
categories; difficulty drawn from the hard end (face/edge/object-count score).
Agent config identical to wave6/8: `qwen3-vl-30b`, low effort, grounded,
decompose, max-steps 35, 900s timeout.

## Headline scores (best-per-job)

| | n | mean | min | max |
|---|---|---|---|---|
| FreeCAD | 32 | **58.0** | 40 | 74 |
| Blender | 18 | **82.2** | 0 | 100 |
| Overall | 50 | **66.7** | | |

For reference, Wave-8 (different, somewhat easier assets) was FC 55.2 / BL 82.2
/ overall 69.6. On a harder, broader FC set the agent holds ~58 — the FC gap is
structural, not asset-specific.

## CRITICAL reporting artifact: "8 failed" is mostly false

`summary.json` reports 41/50 succeeded, 8 failed, 1 timed_out. But all 8
"failed" jobs are Blender jobs that hit `agent_loop_detected` (rc=1) **after
already building a correct model**:

| job | status | eval score |
|---|---|---|
| bl__51_gear_like | failed | **100.0** |
| bl__23_stairs | failed | 88.6 |
| bl__54_chair_5_part | failed | 87.4 |
| bl__53_table_4_legs | failed | 85.6 |
| bl__28_bool_union_chain | failed | 81.6 |
| bl__08_plane_grid | failed | 80.0 |
| bl__03_icosphere | failed | 72.4 |
| bl__55_house_assembly | failed | 0.0 (genuinely bad) |

So loop-kill (restored for BL by the wave-8.1 revert) is doing its job —
stopping the agent from overwriting a good build — but it terminates with
`rc=1`/`status=failed` even when the current geometry is correct. Two
consequences:
1. **Status is misleading**: 7 of 8 "failures" are high-quality outputs. Real
   BL success rate is ~17/18, not 10/18.
2. The deeper issue is **termination recognition**: the agent built the right
   thing but could not emit `terminate`, so it looped until killed.

**Fix**: when loop-kill fires, exit 0 and mark `status=loop_killed` (distinct
from `failed`); the trajectory's geometry is valid. Also feed completion
evidence to the agent (see S2 below) so it terminates on its own.

## Quantified behavioral failure modes

Across trajectories (best per job):

| signal | FreeCAD (n=32) | Blender (n=18) |
|---|---|---|
| did NOT self-terminate (loop-kill / max_steps) | 17/32 | 10/18 |
| high code-repetition (≥50% duplicate evals) | **30/32** | 15/18 |
| stuck emitting ≥3 identical evals in a row | **29/32** | 12/18 |
| multi-primitive with NO `translate` (stacking at origin) | **19/32** | 1/18 |
| python exec errors | 0/32 | 0/18 |

Zero exec errors: the emitted code always *runs*. The failures are entirely
about **what** code is emitted, not syntax.

## Failure Mode 1 — Non-adaptive repetition (DOMINANT, FC)

Every one of the 10 worst FC jobs has `distinct_code = 1`: the agent emits a
single python_eval payload and re-issues it **identically** 2–35 times, never
changing a parameter, until `max_steps` or it gives up and terminates on a wrong
build.

Root cause, verbatim from `wall_hung_toilet` (score 39.6, 35× identical):
> "The previous attempts used identical code, but the viewport may not have been
> framed. Emitting the same build command again with a subsequent frame_view
> will ensure the geometry is visible."

The agent's causal model is **"build doesn't match goal → viewport is stale →
re-run same code"**, never **"→ my code is wrong → change it"**. The Wave-8
anti-loop directive IS delivered (rationales cite "previous attempts used
identical code") but the agent rationalizes around it with the viewport-staleness
excuse. The directive does not change behavior; `distinct` stays 1.

## Failure Mode 2 — Bounding-box approximation of complex single objects (FC)

For organic / high-face-count single parts (toilet, faucet, hinge, roof sheet),
the agent reads `bbox_mm` from goal metadata and builds **one `Part.makeBox`** of
those dimensions — a featureless block standing in for the real surface geometry.
Example: `wall_hung_toilet` → `Part.makeBox(458, 778, 1119)`, scored 39.6 (a box
is not a toilet). The decompose metadata gives a bbox but no path to reconstruct
the actual shape, and the agent has no refinement loop. This caps complex
single-object FC scores at ~40–55.

## Failure Mode 3 — Multi-part stacking at origin (FC, 19/32)

When a part decomposes into multiple primitives, the agent emits N
`makeBox`/`makeCylinder` calls but omits `.translate()` / `Placement`, so all
parts pile at (0,0,0). Visually it looks like one object → agent infers "only 1
built" → loops (feeds Mode 1). The decompose block lists per-part absolute world
origins but the agent does not apply them. Extreme case: `mit_e_vent` (82 parts)
emitted 338 primitive calls in one block, all at origin, repeated 26×, timed out
at 53 steps (score 48.4).

## Termination breakdown (succeeded jobs)

| terminated_by | n | mean score |
|---|---|---|
| agent (self-terminated) | 25 | 68.5 |
| max_steps | 16 | 61.2 |
| agent_loop_detected | 8 | (BL, high — see artifact above) |

Self-termination correlates with higher scores, but even self-terminated FC jobs
average only ~68 because they terminate on bbox-approximations (Mode 2).

## Difficulty does NOT predict FC score

easy FC (diff<250): mean 56.7 · hard FC (diff≥250): mean 59.9. The failure is
behavioral (repetition / no-refinement), not complexity-driven — the agent fails
the same way on simple and hard parts.

## Recommended Wave-9 fixes (now evidence-backed)

1. **S2 — post-build state injection** (highest leverage). After each
   python_eval, extract `App.ActiveDocument` object count + per-object bbox and
   inject `AGENT_STATE: {n_objects, bboxes, all_at_origin?}`. Directly refutes
   the viewport-staleness excuse (Mode 1) and exposes stacking (Mode 3): the
   agent sees "14 objects, all bbox-centered at (0,0,0)" and knows to add
   translations. Addresses Modes 1 & 3.
2. **A2 — named-failure repair recipes**. When AGENT_STATE shows
   "objects built but overlapping at origin" → inject
   `MISSING_TRANSLATIONS: add .translate(Vector(x,y,z)) per part`. Codifies
   "change the code, don't retry."
3. **loop_kill → status=loop_killed, exit 0**. Stop reporting correct BL builds
   as `failed`. Cheap, immediate; fixes the reporting artifact.
4. **Decompose-prompt fix**: emphasize applying per-part absolute origins with a
   worked `.translate(Vector(*origin))` example. Cheap; targets Mode 3.
5. **(Stretch) shape refinement for complex single objects** (Mode 2): a
   bounding-box is the agent's ceiling without a way to add features. Needs a
   different goal representation (multi-view or per-feature decomposition), not
   just bbox. This is the cap on FC organic parts and is not addressed by 1–4.
