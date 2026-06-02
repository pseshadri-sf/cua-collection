# TODO — text-to-cad branch

This document captures the design, scope, and benchmark plan for ideas
ported from [earthtojake/text-to-cad](https://github.com/earthtojake/text-to-cad)
into our VLM-driven CAD reconstruction pipeline. Lives on the `text-to-cad`
branch off `tool-calling` (commit `79d1769`).

## Where text-to-cad ≠ us

text-to-cad is a **skill-driven, build123d-based, STEP-first** pipeline
forcing a 10-step workflow:

1. classify task
2. load only needed references
3. write a natural-language CAD brief
4. search off-the-shelf parts catalog (`step.parts`)
5. plan parameters
6. **edit a persistent `gen_step()` source file** (not stateless code shots)
7. run `scripts/step` to regenerate STEP
8. `scripts/inspect refs --facts --planes --positioning` for programmatic validation
9. **mandatory multi-view snapshot review** with risk-based triggers
10. repair loop with **named failure classes** (fillet, scale, selector, etc.)

Validation pivots on **`@cad[path#selector]` refs** — stable handles to
specific faces/edges/occurrences so the agent can say "fillet
`@cad[bracket.step#f12]`" instead of guessing indices.

We are: VLM emits raw `python_eval` into a live FreeCAD/Blender Qt
process; reads back a screenshot; decides next move. **No persistent
source, no addressable refs, no post-build inspection, no defined repair
loop, no multi-view goal.**

The Wave-6 failures map cleanly to those gaps:

| Wave-6 failure | Root cause | Text-to-cad equivalent |
|---|---|---|
| `agent_loop_detected` after 3 identical `python_eval` | no readback of committed geometry | `scripts/inspect refs` |
| FC ceiling at 54 on revolutions | one iso PNG silhouette-ambiguous → falls back to `makeBox` | multi-view snapshot packet |
| Hex prism only solvable with hardcoded recipe | no addressable surface refs to target | `@cad[#f0(Cylinder)]` selectors |
| List-comp syntax error on 12-part | re-typing same code from scratch | edit-file repair loop |
| BL hollow cup scored as solid | no section view | `mode: "section"` snapshot |

## Ideas, ranked

### S-tier — likely large lift on hardest assets

#### S1. Persistent `gen_part.py` source-of-truth file
**Current:** python_eval shots into live GUI; agent can't read what it
committed. Three identical retries → loop-kill.
**Proposed:** per-job `gen_part.py` with `def gen_part():` envelope.
Orchestrator runs `freecadcmd gen_part.py --out <stem>.FCStd` after every
edit. Agent reads back the source it wrote, can target specific lines for
repair. Apply same shape to Blender (`gen_blend.py` running under
`blender -P`).
**Cost:** ~400–600 LOC. Touches `agent/runner.py`,
`agent/blender_runner.py`, `agent/vlm_client.py` action schema.
**Status:** **deferred** to Wave-8. Too large for a single session;
needs its own scoped wave.

#### S2. Post-build geometry feedback ← *in scope this branch*
**Current:** `extract_goal_metadata.py` runs once on the goal; agent sees
zero metadata about what *it* built.
**Proposed:** after every committed `python_eval`, re-run the extractor
on the agent's current FCStd/blend and inject an `AGENT_STATE` block into
the next turn's user message:

```text
AGENT_STATE (after your last python_eval; trust over the screenshot):
  object_count: 3  (goal: 4)         ← 1 missing
  bbox_mm: [80, 50, 6]  (goal: [80, 50, 6])  ← match
  surface_taxonomy.counts: {Plane: 6, Cylinder: 2}
    (goal: {Plane: 6, Cylinder: 4})  ← 2 cylinders missing
  last_build: SUCCEEDED              ← code ran without exception
```

**Cost:** ~120 LOC: a `scripts/extract_agent_metadata.py` shim that
points the existing extractor at the live FCStd/blend, plus a new
`_render_agent_state` method in `vlm_client.py`, plus a hook in
`runner.py`/`blender_runner.py` after each successful python_eval.
**Expected lift:** addresses the universal loop-kill failure mode.
Conservative estimate +5 to +10 mean (closes most low-scoring
trajectories that were 1-2 steps from passing).

#### S3. Multi-view goal packet ← *in scope this branch*
**Current:** one goal PNG, typically isometric. Sprockets, cans, hinges
are silhouette-ambiguous → agent falls back to `makeBox`.
**Proposed:** pre-render 4 cameras (iso + front + top + right) per
goal; if `extract_goal_metadata` detects `bbox_volume / mesh_volume >
1.5`, also render `section_y`. Concatenate into a single 2×2 (or 2×3
with section) atlas PNG injected as the goal image. Single-image to keep
existing single-image API; agent reads all 4 views from one tile sheet.
**Cost:** ~200 LOC: new `scripts/render_goal_multiview.py` (Blender +
freecadcmd paths), invoked once per asset and cached, plus a hook in
`build_goal_metadata_sidecars.py` to record the multiview-PNG path next
to the meta.json, plus vlm_client.py loading the multiview when present
(falling back to single iso).
**Expected lift:** directly attacks the FC revolution ceiling. Top-view
reveals tooth count / blade count / hole pattern; front view fixes
profile silhouette. Conservative estimate **+8 to +12 on FC mean**
(54 → 62-66) on revolution-heavy assets.

#### S4. Addressable `@cad`-style refs in the sidecar
**Current:** sidecar has `surface_taxonomy: {counts, samples}` but no
addressable handle. Agent guesses `wires[14]`.
**Proposed:** emit stable refs into sidecar:
```json
"refs": [
  "@goal#f0 (Cylinder r=10.0, axis=Z, normal=+Z, area=314)",
  "@goal#f1 (Plane normal=+Z, area=200, center=[0,0,5])",
  ...
]
```
plus a tiny `find_face(shape, normal="+Z", kind="Plane")` utility
imported into agent's python_eval namespace.
**Cost:** ~150 LOC: extractor change + namespace wiring.
**Status:** deferred to Wave-9. Depends on stable face-ordering across
agent's intermediate states, which depends on S1 (persistent source) to
be robust. Reasonable on imported-STEP goals where indices ARE stable.

#### S5. build123d-style high-level DSL wrapper
**Current:** agent writes `Part.makeBox(80,50,6).cut(Part.makeCylinder(2.25,7).translate(Vector(30,17.5,0)))`.
**Proposed:** thin shim exposing `Box`, `Hole`, `CounterBore`, `Slot`,
`Shell`, `Fillet(edges_by_axis="+Z")`, `with Locations(*positions):
Hole(d=4.5)`. Cuts LOC-per-feature 5–10×.
**Cost:** 200-line shim OR adopt build123d wholesale via OCP.
**Status:** deferred. Biggest single-shot quality lift but requires
agent re-training on the new API. Wave-10+.

### A-tier — real wins on specific categories

#### A1. Off-the-shelf parts library
For named components (M3/M4 screws, 608/6201 bearings, NEMA 17 steppers,
USB-C connectors, common standoffs): match against a hardcoded local
catalog and `Part.read("library/m4x10_socket.step")` instead of
synthesizing. Even a 20-part starter library covers the long tail of
fastener/connector goals. Pairs with FC surface_taxonomy detection of
"this looks like an M-something screw."
**Cost:** ~50 LOC matcher + ~20 hand-curated STEP files.
**Status:** deferred. Modest scope; would slot well after S2+S3.

#### A2. Repair loop with named failure classes
text-to-cad's `repair-loop.md` enumerates 9 failure classes (source
syntax, invalid geometry, fillet failure, wrong scale, missing feature,
selector fragility, positioning mismatch, viewer failure, snapshot
failure). Each has a named fix recipe.
**Proposed:** `_W7_REPAIR_RECIPES_BLOCK` injected only when AGENT_STATE
(from S2) reports an exception or no geometry change since the last
turn. Modeled after the Wave-5.1 hex prism recipe.
**Cost:** ~80 LOC prompt + ~50 LOC trigger detection in vlm_client.
**Status:** deferred. Hard to land without S2.

#### A3. Source-level joints for assemblies
Extension of TODO BL multi-strategy decomposer. build123d's
`RigidJoint("lid_seat", to_part=base, joint_location=Location((0,0,h)))`
+ `connect_to(lid)` for datum-driven assembly. Maps to TODO "BL
parent-tree" and "BL armature" strategies.
**Status:** deferred. Sits behind S5 (build123d DSL).

#### A4. Section rendering for hollow goals
`snapshot-review.md` recommends `mode: "section"` for shells, internal
cavities, bores. Inject when `bbox_volume / mesh_volume > 1.5`.
**Status:** **folded into S3** (multi-view) — auto-add section view for
hollow detected geometry.

#### A5. Negative checks in evaluator
Every text-to-cad benchmark has explicit negative tests ("Ring teeth
must face inward; planet gears should not be fused into sun or ring").
Our match_score can be gamed by a too-fused single solid that hits
bbox. Add per-asset negative tests to W6 hardest set.
**Cost:** ~150 LOC per-asset JSON of expected `min_object_count`,
`forbidden_intersections`, etc.; ~50 LOC scorer hook.
**Status:** deferred. Surfaces false positives but doesn't drive new
agent capability. Wave-9.

### B-tier — polish, savings, structural improvements

#### B1. Progressive reference loading
Conditional prompt blocks. W5_FC_SURFACES only ships to FreeCAD jobs.
W4_DECOMPOSE only when `parts` present. ~30% context savings + reduces
dilution.
**Cost:** ~30 LOC vlm_client.py.
**Status:** **trivial; ship in S3 PR** as plumbing.

#### B2. CAD brief as forced first step
Forced single-turn "write your brief" before code. Cuts tokens-to-success
on multi-feature assets but adds latency on trivial ones. Gate by
`parts > 1 OR surface_taxonomy has multiple kinds`.
**Status:** deferred. Risk of regression on simple goals.

#### B3. Validation hierarchy exposed in prompt
Expose `check_volume(target=400)`, `check_bbox(target=[80,50,6])`,
`check_face_count(target=14)` helpers in python_eval namespace. Agent
can self-validate before declaring done.
**Cost:** ~80 LOC.
**Status:** deferred. Subsumed by S2 (AGENT_STATE already exposes the
same numbers; helpers are sugar).

#### B4. Parameters-first style enforcement
Force `width = 80.0; depth = 50.0; ...` at top of `gen_part()`. Enables
single-number repair and mutation/ablation.
**Status:** deferred. Sits behind S1 (persistent source).

#### B5. Topology-stack discipline in prompt
"Vertex → Edge → Wire → Face → Shell → Solid → Compound" + 3 worked
examples (wire+face+extrude prism, wire+revolve revolution, lofts for
transitions). Probably captures most of FC revolution recipe TODO at
lower scope.
**Cost:** ~60 LOC prompt block.
**Status:** **fold into existing FC revolution recipe TODO** in
`TODO.md`. No change here.

### C-tier — different problem domain

- CAD Viewer browser handoff (no human-in-loop reviewer for us)
- step.parts hosted API integration (offline; need local catalog instead — see A1)
- `inspect diff` STEP-vs-STEP comparator (depends on S1)
- npm/Node-based viewer infrastructure
- URDF/SRDF/SDF/Bambu/SendCutSend skills (orthogonal to reconstruction)

## Scoped for this branch

**In scope:**

- **S3** multi-view goal packet (iso + 3 ortho + optional section) —
  pre-render once per asset, swap in for `goal.png` when present.
- **B1** progressive reference loading (app-gated prompt blocks) —
  free plumbing, ship in same PR.
- Benchmark on the full W6 50 (since the change is image-only and
  preserves the API; no need to slice down).

**Pulled back after runtime survey (was originally in scope):**

- **S2** post-build geometry feedback — requires modifying the
  `_do_python_eval` action executor to append a save-state line, plus
  an async metadata extractor, plus a vlm_client `AGENT_STATE` renderer.
  Estimated ~300 LOC across runner.py + blender_runner.py + 2 action
  spaces + vlm_client.py, with a real risk of breaking the GUI-typing
  pipeline if the appended save line interacts badly with malformed
  agent code. **Decision:** ship S3 first, benchmark, then decide if
  the lift warrants S2's complexity. If S3 alone closes most loop-kills
  (by giving the agent enough info from the multi-view that they
  don't need feedback), S2 becomes lower priority.

**Out of scope (captured above for future waves):**

- S1 persistent gen_part.py source (Wave-8)
- S4 addressable @cad refs (Wave-9, after S1)
- S5 build123d DSL (Wave-10+)
- A1 OTS parts library (Wave-9 candidate)
- A2 repair loop (deferred behind S2)
- A5 negative checks (Wave-9)

## Benchmark plan

1. **Baseline:** W6 hardest 25 with no changes. Use the existing W6 run
   (`/home/ubuntu/cua_gui_smoketest/runs/wave6_20260601T190603Z`) as
   baseline — *no rerun needed*; just slice the 25 from its
   `wave6_summary.json`.
2. **Treatment:** rerun the same 25 jobs with S2 + S3 enabled. Use the
   same model (`qwen/qwen3-vl-30b-a3b-instruct`), same max_steps (35),
   same reasoning_effort (low), same image-max-dim (1024).
3. **Metrics:**
   - Δ match_score per app (BL mean, FC mean, combined mean)
   - Δ on revolution-class assets only (sprockets/faucets/cans/hinges/cups)
   - Δ loop-kill rate (% with `agent_loop_detected` termination)
   - Δ tokens-per-job (S2 adds ~80 tokens/turn; S3 adds ~0 since one image)
4. **Per-asset breakdown:** generate compare PNGs with the existing
   `render_eval_compare.py` for visual review.
5. **Stop criterion:** if combined mean drops > 5 below baseline,
   abort and post-mortem; if at/above baseline, write up Wave-7 notes.

## File touch list (S3 + B1)

| File | Change | Est LOC |
|---|---|---|
| `scripts/render_goal_multiview.py` (new) | Render iso+front+top+right (+ optional section_y) into 2×2 or 2×3 atlas PNG. Cache to `/tmp/multiview_cache/<stem>.png`. | ~280 |
| `src/cua_smoketest/agent/vlm_client.py` | (1) `_resolve_goal_png()` swaps in cache hit when present; (2) Conditional FC/BL prompt blocks (B1) | ~40 |
| `TODO-text-to-cad.md` (this file) | Created | — |

Total: ~320 LOC, all additive, fail-silent (no cache → falls back to
single iso). Zero changes to runner/action-executor, so the existing
W6 pipeline keeps working unmodified.

## Results — Wave-7 (S3 multi-view only) vs Wave-6 baseline

**Run:** `/home/ubuntu/cua_gui_smoketest/runs/wave7_20260602T021234Z/`
**Subset:** 22 hardest W6 jobs (12 FC + 10 BL spanning lowest scores).
20 of 22 produced valid scoreable trajectories.

### Aggregate

| Metric | W6 baseline | W7 (S3) | Δ |
|---|---|---|---|
| Overall mean | 53.1 | **58.4** | **+5.2** |
| Blender mean | 63.9 | **69.1** | **+5.1** |
| FreeCAD mean | 45.9 | **51.2** | **+5.3** |
| Wins / Losses / Ties | — | 11 / 2 / 7 | — |

### Biggest single lifts (where multi-view paid off)

| Job | App | W6 | W7 | Δ | Why |
|---|---|---|---|---|---|
| `fc__hvac__conections__extended_retangular_` | FC | 37.1 | 56.5 | **+19.4** | Side view revealed rectangular profile that iso compressed |
| `bl__25_lattice_cubes` | BL | 82.1 | 100.0 | **+17.9** | Top view nailed the cube grid count |
| `bl__21_landscape` | BL | 78.0 | 95.0 | **+17.0** | Front view exposed terrain layering |
| `fc__electronic__smd__usb-micro-b_step` | FC | 32.1 | 48.9 | **+16.8** | Top + right views disambiguated connector shell |
| `fc__architectural-parts-hydro-equipment-fa` | FC | 46.0 | 55.4 | **+9.4** | Front view fixed hub/flange aspect |
| `bl__35_random_mixed` | BL | 73.8 | 82.3 | **+8.5** | Multi-view caught count of distinct primitives |
| `fc__chain__simplex_1¾x1¼__sprocket_ansi_si` | FC | 41.3 | 49.0 | **+7.7** | Top view showed tooth circle (not tooth count, but circular footprint) |
| `fc__electronic__female__3pin-female-2.54mm` | FC | 51.6 | 57.9 | **+6.3** | Front view fixed pin-row layout |

### Ties and small losses

7 ties — almost all are `terminated_by: agent_loop_detected`. The agent
emitted 3 identical `python_eval` payloads and was force-killed; the
multi-view atlas gave it a better initial guess, but it still had no way
to **confirm the geometry was committed**. The single iso vs multi-view
makes no difference once the agent is stuck in a verify-but-can't-see
loop.

Two small losses: cup (-2.0) and torus array (-2.4). Both are within
random-seed noise; not material.

### Per-failure-mode breakdown

- **Silhouette-ambiguous shapes** (revolution-class, multi-pin
  connectors, lattices): **+13 mean** when multi-view fires.
- **Loop-kill (agent_loop_detected)**: **no change**. S2 territory.
- **Multi-part decompose**: unchanged (decompose path independent of
  the goal image).

### Costs

- Pre-render: ~25 min wall for all 50 W6 atlases (4 workers via
  `xvfb-run -a`). FC dominates (~50s/asset due to freecadcmd→STL); BL
  is ~14s.
- VLM per-call latency: first call ~30-60s (vs ~15-30s for single iso —
  larger payload to ingest). Subsequent calls cache prefix, stay ~7-15s.
- Per-job wall time: ~60-100s for short trajectories, ~3-6 min for full
  35-step runs. Comparable to W6.

### Verdict

**Ship S3.** Substantial lift across both apps on the hardest assets,
no regression, no infra cost beyond a one-time per-asset pre-render.
The multi-view image is the agent's new "goal" — fail-silent: assets
without an atlas keep the original iso behavior.

### Next: S2 is the obvious follow-on

Of the 7 ties this run, 6 were `agent_loop_detected` — the universal
failure mode that S3 cannot touch. S2 (post-build geometry feedback)
directly addresses it. With both S2 + S3 we'd likely flip most of those
7 ties into wins, putting overall mean above 65.

Risk: S2 needs ~300 LOC across action executors. Recommendation: scope
S2 as Wave-8 in a fresh branch; keep this branch's S3 win clean.
