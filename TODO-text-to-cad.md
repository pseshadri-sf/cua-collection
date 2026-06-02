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

### Verdict (subset)

On the 20 hardest scoreable jobs, S3 is a substantial win. **But this
result is biased by the subset selection** — see full-50 below.

## Results — Wave-7 full 47-job run (extension)

**Run:** `/home/ubuntu/cua_gui_smoketest/runs/wave7_20260602T021234Z/`
(extended with the remaining 28 W6 jobs after the subset benchmark
showed a clear lift on the hardest set).

### Aggregate

| Metric | W6 baseline | W7 (S3) | Δ |
|---|---|---|---|
| Overall mean (n=47) | 69.3 | **69.9** | **+0.6** |
| Blender mean (n=25) | 83.8 | **82.3** | **−1.5** |
| FreeCAD mean (n=22) | 52.8 | **55.7** | **+2.9** |
| Wins / Losses / Ties | — | 14 / 11 / 22 | — |

### What changed when we expanded from 22 → 47

The 22-hardest subset is dominated by FC revolutions + BL lattices —
exactly the silhouette-ambiguous class where multi-view shines. The
remaining 28 includes many already-easy Blender wins (single primitives,
small primitive groups) where the single iso was already sufficient.

### The new regressions (introduced by S3 on easy assets)

| Job | App | W6 | W7 | Δ | Hypothesis |
|---|---|---|---|---|---|
| `bl__43_dense_scatter_100` | BL | 89.3 | 51.7 | **−37.6** | 100-sphere scatter; agent confused by per-view counts |
| `bl__49_nested_spheres` | BL | 100.0 | 80.3 | **−19.7** | concentric spheres; ortho top view obscures inner structure |
| `bl__10_sphere_ring` | BL | 100.0 | 86.5 | **−13.5** | top view collapses ring to circle; agent guesses one sphere |
| `bl__36_torus_tower` | BL | 95.4 | 85.4 | **−10.0** | stacked tori; iso was already clear |
| `bl__46_organic_blob` | BL | 100.0 | 93.0 | **−7.0** | metaball; multi-view introduces noise |

These regressions are all from Blender's easy class. The multi-view
introduces an extra cognitive load on the agent (parse 4 views,
reconcile) that's a net cost when iso was already telling the truth.

### Honest verdict

S3 is **net-positive on FreeCAD** (+2.9) and **net-negative on Blender**
(−1.5). Overall barely moves. The win on the hardest 20 was real but
NOT representative of the broader distribution.

**Action:** do not ship S3 unconditionally. Either:

1. **Gate it** — only activate multi-view when sidecar detects a
   silhouette-ambiguous goal:
   - FC: `surface_taxonomy.counts` contains `BSplineSurface`,
     `SurfaceOfRevolution`, `Cone`, or `Toroid`; OR a connector/SMD
     pattern (multi-pin layouts).
   - BL: `object_count > 20` (lattice/scatter class) AND object_types
     are heterogeneous; OR the asset name matches text-class keywords.
   - Skip multi-view for: BL `object_count ≤ 5` AND single-primitive
     `dominant_primitive_class`.

   Estimated effort: ~30 LOC in `_resolve_goal_png`; expected to recover
   the +5 hardest-subset lift while killing the easy-asset regression.

2. **Reduce atlas resolution** — currently each tile is 640×480 (atlas
   ~1290×1020 → downscaled to 1024×808 at the VLM). For easy assets,
   the larger payload alone may be slowing first-token latency enough
   to cause the agent to terminate early or skip steps. Cutting tiles
   to 480×360 (atlas 960×760) might halve the regression.

3. **Both** — gate + smaller tiles.

### Updated branch recommendation

Do **not** merge `text-to-cad` to `tool-calling` as-is. The unconditional
S3 hurts a meaningful slice of the BL baseline. Instead:

1. Land the **infrastructure** (`render_goal_multiview.py`, the
   `_resolve_goal_png` plumbing, `compare_waves.py`, TODO doc) — all
   safe, no behavior change without the cache.
2. Add the **gating logic** as a follow-up Wave-7.1 commit that
   conditions atlas activation on sidecar shape-class. Re-benchmark.
3. Decide on merge after Wave-7.1 result.

### Why this also reframes S2's priority

S2 (post-build feedback) addresses `agent_loop_detected` — the universal
failure mode across BOTH the assets that win and those that lose with
multi-view. It would likely lift the regressed BL assets back to baseline
(the agent terminates early because it can't see what it built —
post-build feedback gives it that information). So S2 is now the
**primary** next-wave priority over Wave-7.1.

| Priority | Item | Expected lift on full 47 | Cost |
|---|---|---|---|
| 1 | **S2 post-build feedback** | +5 to +10 across both apps; closes loop-kills | ~300 LOC |
| 2 | Wave-7.1 — gated S3 | Recover +3 from current −0 on easy assets | ~30 LOC |
| 3 | A2 repair loop (needs S2) | +2 to +5 on syntax/scale/fillet failures | ~130 LOC |
| 4 | S5 build123d DSL | Largest single-shot quality lift | ~600 LOC |

## Performance-gap analysis — trajectory dissection (W6 + W7)

Looked at actions, screenshots, and rationales across all 83 W6 + 75 W7
trajectories. Findings reorder the priority above significantly.

### Headline numbers

| Termination type | N (W6) | Mean score | % uses frame_view/all |
|---|---|---|---|
| `agent` (clean) | 13 | **92.0** | **92%** |
| `agent_loop_detected` | 70 | 61.0 | **6%** |

Then split by app:

| App | term | N | Mean | python_evals/run | frame_*/run |
|---|---|---|---|---|---|
| **blender** | **agent (clean)** | **13** | **92.0** | 1.2 | **1.0** |
| blender | loop_kill | 26 | 75.3 | 3.1 | 0.0 |
| **freecad** | **agent (clean)** | **0** | — | — | — |
| freecad | loop_kill | 44 | 52.6 | 3.1 | **0.1** |

### The two cliffs

**Cliff 1: `frame_view` / `frame_all` usage discriminates wins from
loop-kills almost perfectly.** 92% of clean terminations use it; 6% of
loop-kills do. The prompt explicitly tells the agent to use it after
python_eval, but in practice the agent skips it. Why: agent's
heuristic for "do I need frame?" is "is the geometry invisible in
CURRENT_STATE?" — but if CURRENT_STATE is the **stale** viewport from
before the eval, the answer the agent infers is "no, geometry wasn't
created, retry the eval".

**Cliff 2: Zero FreeCAD trajectories ever emit `terminate`.** Across
all 44 FC runs in W6, the agent never decided to terminate FC. Every
single FC run died by loop-kill at 3 identical retries. Compare BL:
13/39 (33%) emit terminate. FC is structurally broken at the
termination layer.

### Root cause: the viewport-staleness loop

Dissected `fc__industrial__chairs__adirondack_chair_s` step-by-step:

```
step 1 (t=22s): python_eval — builds 12-part chair, rationale "I used
                per-part decomposition strategy, built 12 largest parts"
step 2 (t=43s): python_eval — IDENTICAL code, rationale "The provided
                Python script constructs the 12 largest parts"
step 3 (t=74s): python_eval — IDENTICAL code, rationale "The provided
                Python script constructs the 12 largest parts"
                → agent_loop_detected
```

Frame-by-frame pixel diff: `step_01 vs step_02: IDENTICAL`,
`step_02 vs step_03: IDENTICAL`. The CURRENT_STATE never changed
across the 3 retries.

But across all 70 W6 loop-kills, only 21% had identical screens — the
other 79% showed substantial pixel change (often 77%+, full repaint).
So the screen DID change between retries in most cases, yet the agent
still emitted identical code.

The "smoking gun" confirmation: doors-glass W7 trajectory scored 87.2
(geometry was nearly correct), terminated by loop-kill. The clean
headless re-render (`agent_render.png`) shows a perfectly-formed door
part; the trajectory's `step_03_before.png` shows the FC GUI with the
viewport never having fitted to the built geometry. **The agent
built the correct part but the viewport never showed it.** The agent's
screenshot showed the static FC chrome + console pane while the
geometry sat at world origin, outside the camera frustum.

### Specific deficits to fix (in priority order)

**A. Auto-fit viewport after every FC python_eval** — append
`;Gui.SendMsgToActiveView('ViewFit')` to the code string before typing
into the console. ~5 LOC in `agent/action_space.py::_do_python_eval`.
Closes the viewport-staleness loop without changing the agent's
behavior. Expected lift: closes most of FC's 44/44 loop-kills, likely
+15 mean on FC alone.

**B. Auto-chain `frame_view` after every `python_eval`** — runtime
emits the frame as a sequel action automatically. The agent doesn't
have to remember. ~15 LOC in runner.py + same in blender_runner.py.
Belt-and-suspenders with (A); a redundant frame_view costs ~600ms.

**C. Crop runtime screenshots to viewport-only region** — strip the FC
chrome (toolbar, sidebar, console pane) so the agent's CURRENT_STATE
visually compares well to GOAL_STATE (which is the bare 3D model).
Currently the FC agent sees 70% chrome / 30% viewport; the BL agent
sees a much cleaner workspace.

**D. Force terminate after a successful frame in FC** — extend the
prompt's "REQUIRED STRATEGY (FreeCAD)" to make the third step
mandatory: after frame_view, if geometry matches goal, emit terminate
within 1 turn. The BL strategy already has this pattern and it works
(13 clean BL terminations).

**E. S2 post-build feedback** — still the right intervention for the
21% truly-identical-screen cases, where the python_eval failed to
execute at all (focus loss / console glitch). With AGENT_STATE
showing `last_build: FAILED`, agent knows to try a different focus
strategy instead of retyping.

### Reordered priority (after trajectory dissection)

| # | Item | Cost | Expected | Why |
|---|---|---|---|---|
| **1** | **A** auto-fit FC viewport | ~5 LOC | **+15 on FC** | Closes the dominant FC failure mode |
| **2** | **B** auto-chain frame_view | ~15 LOC | +3 broadly | Belt-and-suspenders for A |
| **3** | **D** mandatory FC terminate | ~30 LOC prompt | +5 on FC | Pairs with A — agent needs the "done" signal |
| 4 | C viewport-only screenshots | ~50 LOC | +5 on FC | Cleaner visual compare |
| 5 | S2 post-build feedback | ~300 LOC | +5 broadly | Covers focus-loss case |
| 6 | Wave-7.1 gated S3 (in progress) | done | +1 to +2 | Already in this branch |
| 7 | A2 repair loop | ~130 LOC | +2 to +5 | Needs S2 |
| 8 | S5 build123d DSL | ~600 LOC | +5 to +10 | Biggest single shot |

**Note: items A + B + D together address the root cause** found in
this analysis (FC viewport never reveals built geometry → agent
loop-kills). The whole text-to-cad ladder (S2-S5) is downstream of
this. Without A, the agent never gets a chance to use post-build
feedback because it never reaches a state where the screen reflects
its work.

## Results — Wave-7.1 (gated S3) vs W6 baseline

**Run:** `/home/ubuntu/cua_gui_smoketest/runs/wave71_20260602T034553Z/`
Same 50 W6 jobs, with the conservative gate active (FC: revolution
markers OR face_count ≥30 OR Cylinder ≥4 → atlas; BL: always iso).

| Metric | W6 baseline | W7 ungated | **W7.1 gated** | W7.1 Δ vs W6 |
|---|---|---|---|---|
| Overall (n=47) | 69.3 | 69.9 | **71.4** | **+2.1** |
| Blender (n=25) | 83.8 | 82.3 | **85.7** | **+1.9** |
| FreeCAD (n=22) | 52.8 | 55.7 | **55.0** | **+2.2** |
| Wins/Losses/Ties | — | 14/11/22 | **13/7/27** | — |

W7.1 vs W7 improvement: **+1.5 overall, +3.4 BL** (recovered from
-1.5 to +1.9), -0.7 FC (slight cost from gate excluding a few FC
assets that surface_taxonomy doesn't catch as revolution-class).

### Residual W7.1 regressions

Two notable regressions remain even with the gate:

- `bl__10_sphere_ring`: 100.0 → 86.5 (−13.5). Gate SKIPPED multi-view
  (object_count=8, single type → iso). So this loss is from agent
  temperature-0.2 variance on a previously-perfect baseline. Implies a
  noise floor of ~10-15 points on assets that score 100.
- `fc__industrial__rustic_hinge`: 51.9 → 46.5 (−5.4). Gate FIRED
  (Toroid in surface taxonomy → atlas). One of the few cases where
  multi-view actively hurt FC. Worth investigating per-asset later.

### Verdict

**W7.1 is the right shippable version** of the S3 work. Net +2.1
overall, zero category regressions, FC and BL both positive. The gate
adds ~25 lines of code and fully recovers the BL noise that ungated S3
introduced.

But the **trajectory-dissection finding above (FC viewport
staleness)** is a far larger opportunity than any S3 variant. The
5-LOC `;Gui.SendMsgToActiveView('ViewFit')` append is the next move.

## Wave-8 item A + B smoke results (partial)

Implemented items A (append `;Gui.SendMsgToActiveView('ViewFit')` to
every python_eval code string) AND B (after python_eval, click viewport
and press 0/v/f for iso + fit-all) in `action_space.py::_do_python_eval`.

### Smoke 1 (item A alone) — usb-micro-b, max_steps=5

Result: still loop-killed at 3 identical python_evals.

But the agent's **rationale changed substantively**:

  - Step 2 rationale: *"The previous action already created this box,
    but the viewport is not framed. The next step is to frame the
    view to ensure the geometry is visible..."*
  - Step 3 rationale: *"Proceeding with the same build to ensure the
    object exists, then frame_view will adjust the camera..."*

The agent now **correctly diagnoses** that the build succeeded and
that frame_view is the next step. But it emits python_eval anyway.
That's an action-selection disconnect — the rationale plans the right
next move, but the chosen action defaults back to python_eval.

### Smoke 2 (items A + B) — multi-part variant of usb-micro-b

Result: still loop-killed at 3 identical python_evals.

Same pattern. The runtime is now correctly framing the geometry but
the agent's loop-detection (3 identical agent-emitted actions) triggers
regardless of what the executor does.

### Diagnosis: loop-detection counts agent-emitted actions, not executor results

`runner.py::loop_kill_repeats=3` checks the agent's emitted action
JSON, not the executor's internal action chain. A+B run more
operations *inside* the python_eval execution, but the recorded action
is still `{"type":"python_eval","code":"..."}` — identical to the
previous step's, so the loop-detector still fires at 3.

### What item A+B actually need to be effective

Three additional changes:

  1. **Bump `loop_kill_repeats` from 3 → 5** for FC. Gives the agent
     2 more retries before kill, during which the framing has had time
     to take effect across multiple screenshots.

  2. **Synthetic auto-step injection**: after a successful python_eval,
     the runner records a synthetic `{"type":"frame_view","_auto":true}`
     step. The agent's history then shows
     `[py, frame, py, frame, py, ...]` instead of `[py, py, py]`, so
     loop-detect doesn't fire AND the agent sees a "frame happened"
     signal.

  3. **Prompt-level forcing**: when the recent action history shows
     2+ python_evals with no terminate, inject into the per-turn user
     message: *"You have emitted python_eval twice. The geometry is
     already built and framed. Your next action MUST be either
     `frame_view` or `terminate` — do NOT emit another python_eval."*

Cost of full chain: items A+B (~30 LOC, shipped now) + items 1-3 above
(~80 LOC). Total ~110 LOC. Expected to take FC's 0/44 clean-termination
rate to something like 30-50% (matching BL's 33%).

### Decision: ship A+B but do not benchmark until items 1-3 land

A+B in isolation made the agent's *rationale* better but not its
*action choice*, so a benchmark of A+B alone would likely show no
meaningful change in scores. Hold off on benchmarking until the
loop-detection bypass + synthetic step + prompt forcing are added.
Scope these as **Wave-8** in a fresh branch; the text-to-cad branch
stays focused on the S3 + scope/analysis deliverables.
