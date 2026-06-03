# Scoping — One-Pass Frontier-Planner Approach

**Branch:** `frontier-onepass` (isolated worktree; main tree stays on `wave-9`
for the running benchmark). This is a SCOPE/design doc — nothing implemented yet.

## Decisions (locked)

- **Variant A — plan-as-guidance** is what we build. The frontier model plans;
  the qwen3-vl-30b executor still decides each action against the live viewport.
- **Downstream action format: `python_eval`.** `build_*` is DEMOTED (see
  reassessment below). `python_eval` gives full expressivity (revolves, lofts,
  booleans — the organic shapes `build_*` cannot express) and already won both
  tiers in wave-10. `build_*` is a strict SUBSET of `python_eval`, so it can only
  cap the custom-shape assets, and its only advantage (per-step trackability) is
  moot while the SLM one-shots the whole `full_code`. Run `build_*` only as a
  cheap control on the pure-primitive assembly subset, and only after a per-step
  verify-repair loop (Variant C) exists to give its trackability something to use.

## Reassessment after wave-10 (why build_* is demoted)

wave-10 (A + python_eval, FC) lifted FreeCAD **58.0 → 65.0 (+7.0)**; S2/A2 was
+0.0. Crucially the single-solid tier moved MOST (+8.8) because the planner used
revolves/booleans the SLM never could — expressivity that lives in `python_eval`
and is absent from `build_*`. Plan-adherence was 34/34: the SLM emits the whole
`full_code` in one action and terminates, so there is no per-step execution for
`build_*`'s trackability to attach to. Net: `build_*` would likely be flat-to-
negative and would forfeit the custom-shape gains. The productive directions are
the OPPOSITE of `build_*`:
1. give the planner MORE expressivity (explicitly prompt revolve/loft/boolean);
2. a per-step verify-repair loop (Variant C) keyed to S2 — the real fix for the
   assembly tier and the one mis-decomposition regression (hvac bend 66→50).
- **Plan structure refined from the [earthtojake/text-to-cad] skill set** — see
  the dedicated section below. The frontier call front-loads text-to-cad's
  "classify → CAD brief → parameter plan → ordered build" so the SLM inherits a
  parameters-first, datum-explicit, failure-class-aware plan.

## Motivation (why this, why now)

The wave-9d failure-mode analysis showed the small VLM's failures are
**planning failures, not execution failures**:

- 0/32 FC trajectories had python exec errors — the emitted code always runs.
- The dominant loss is *what* to build: bbox-approximation of complex single
  solids, multi-part stacking at the origin, and non-adaptive repetition of one
  code block (29/32 emit ≥3 identical evals).
- Even the wave-9 fixes (S2 state injection, A2 translate recipe) only nudged
  behavior (translate usage 3→12) without moving score — the 30B model still
  can't reliably *decompose a goal into correctly-placed parts*.

Decomposition + spatial placement is exactly what a frontier model is good at
and the 30B model is not. So: **call a frontier model ONCE up front to produce
a build plan from the goal image + metadata, then hand that plan to the small
model as authoritative guidance.** One frontier call per asset ("one pass"),
amortized over the whole trajectory — cheap, and it attacks the actual bottleneck.

## Approach overview

```
                    ┌─────────────────────── ONE PASS (step 0, once) ───────────┐
  goal.png  ─┐      │  FRONTIER PLANNER (Claude/GPT-class via OpenRouter)        │
  GOAL_METADATA ────┼─► input: goal image + bbox + surface taxonomy + per-part   │
  decompose parts ──┘  │           decomposition + action-space spec             │
                    │  output: ordered BUILD_PLAN (parts, primitives, dims,      │
                    │          origins, booleans) + per-step rationale           │
                    └───────────────────────────┬───────────────────────────────┘
                                                 │  plan injected as guidance
                                                 ▼
   ┌────────────── PER-TURN LOOP (small VLM: qwen3-vl-30b, unchanged cadence) ───────────┐
   │  system/user prompt now carries BUILD_PLAN + "current plan step k of N"              │
   │  SLM emits one action (python_eval / build_*) toward the current plan step           │
   │  S2 probe reads back document state → AGENT_STATE compared against plan expectation   │
   │  advance plan pointer when the built object matches the step; repair if it doesn't    │
   └──────────────────────────────────────────────────────────────────────────────────────┘
```

The frontier model owns the **plan** (decomposition + placement — the hard part).
The small model owns **execution + local adaptation** (typing code into the GUI,
reacting to viewport/state, minor repairs). This keeps the existing GUI-driving
machinery and per-turn visual grounding while removing the planning bottleneck.

## Where it plugs into the existing code

- `vlm_client.py::OpenRouterVLMClient.next_action()` (line ~1076) builds the
  per-turn message; `_render_grounded_metadata()` (~1111) already injects
  GOAL_METADATA + the `_W4_DECOMPOSE_BLOCK`. The plan becomes a sibling block
  (`BUILD_PLAN`) added to `user_text` / system prompt.
- `runner.py::AgentTrajectoryRunner.run()` has a natural step-0 slot (the null
  init step) to make the single frontier call before the loop starts.
- The plan integrates with the wave-9.1 **S2 AGENT_STATE** readback: after each
  build the probe reports the real object bboxes, which we compare to the
  *current plan step's* expected bbox to decide advance-vs-repair.

## Frontier call — design

**When:** once, at step 0, before the per-turn loop. Result cached to
`<output_dir>/build_plan.json` so reruns/replays are deterministic and the plan
is auditable.

**Input to the frontier model:**
- goal image (the same iso/multi-view atlas the SLM gets — `_resolve_goal_png`).
- GOAL_METADATA: bbox_mm, object_count, surface_taxonomy, volume.
- Per-part decomposition table (when `parts` present in the sidecar) — bbox +
  absolute origin per part.
- The action-space spec (build_box/cylinder/sphere/torus, compound/fuse,
  python_eval) so the plan is expressed in primitives the SLM can actually emit.

**Output schema (STRUCTURED, validated) — refined from the text-to-cad skill
workflow.** The frontier model front-loads text-to-cad steps 1/3/5/6 (classify,
CAD brief, parameter plan, source) so the plan arrives parameters-first and
datum-explicit:

```json
{
  "brief": "EUR-1 pallet, 1200x800x144 mm. 3 top deck boards + 5 lead boards on
            9 blocks on 3 bottom stringers. Origin at base-center, +Z up.",
  "shape_class": "assembly | single_solid | revolve | boolean",
  "parameters": {"L": 1200, "W": 800, "H": 144, "board_t": 22, "block_h": 78},
  "conventions": {"units": "mm", "origin": "base-center", "up": "+Z"},
  "steps": [
    {"i": 1, "name": "deck_top",
     "code": "deck_top=Part.makeBox(L,100,board_t); deck_top.translate(App.Vector(-L/2,-50,H-board_t))",
     "op": "build_box", "dims": [1200,100,22], "origin": [-600,-50,122],
     "why": "top deck board, flush with +Z face"},
    {"i": 7, "name": "pallet",
     "code": "pallet=Part.makeCompound([deck_top, ...])",
     "op": "compound", "shapes": ["deck_top", "..."]}
  ],
  "validation_targets": {"object_count": 17, "bbox_mm": [1200,800,144]},
  "fallback": "if assembly too complex, a single makeBox(1200,800,144) ~ score 50"
}
```

- Each step carries BOTH a ready `code` line (python_eval-first downstream) AND
  the structured `op`/`dims`/`origin` fields (used when we switch to `build_*`).
  One plan, two renderings — no re-planning to compare the two downstream modes.
- `parameters` + `conventions` enforce text-to-cad's **parameters-first** and
  **mm / base-center / +Z** rules; `validation_targets` give the runner concrete
  numbers to reconcile against S2 AGENT_STATE (our "inspect refs" analog).
- Validated (retry on schema mismatch); planner temperature 0; cached to
  `build_plan.json`.

Alternatives considered: (a) free-text NL plan — not machine-trackable;
(b) single ready-to-run python_eval reconstruction — that's Variant B (deferred).

## Integration variants (pick per asset class / ablate)

- **Variant A — plan-as-guidance (DEFAULT).** SLM still decides each action but
  is told the plan + which step it's on. Keeps visual grounding and local
  repair; lowest risk; the SLM can deviate if the viewport disagrees.
- **Variant B — plan-as-script.** Frontier returns the full reconstruction
  (e.g. one python_eval or the ordered build_* list); the SLM/runner just
  executes it, then the SLM does a verify-and-repair pass against the goal.
  Highest fidelity when the plan is right; degenerates to "frontier does
  everything" — tests how much the SLM even matters.
- **Variant C — hybrid (escalation).** Run Variant B's script; if S2/AGENT_STATE
  shows a mismatch (missing parts, wrong bbox), hand control to the SLM in
  Variant-A mode to repair from that state. Best expected quality; most code.

**Locked: build A first** (smallest change, isolates the planning benefit), with
`python_eval` downstream first then `build_*`. Revisit C only if A shows lift on
multi-part assets.

## Refinement from the text-to-cad skill set

text-to-cad ([earthtojake/text-to-cad], `skills/cad/SKILL.md` + references) is a
build123d/STEP pipeline whose agent follows a fixed workflow. Our pipeline is
single-shot `python_eval` into a live FreeCAD GUI — no persistent source, no
post-build inspection, no defined repair loop. The one-pass frontier planner is
how we get most of text-to-cad's *planning* discipline without adopting its
whole build123d/source-file machinery: **the frontier call performs text-to-cad
steps 1–6 once; the SLM loop covers 7–10 using our existing infra.**

### Mapping text-to-cad's 10 steps onto this design

| text-to-cad step | Where it lives here |
|---|---|
| 1. Classify task | `shape_class` field in the plan (single_solid / assembly / revolve / boolean) |
| 2. Progressive reference load | already have app-gated prompt blocks (B1); planner gets metadata + decompose |
| 3. Natural-language CAD brief | `brief` field — frontier writes it from the goal image + metadata |
| 4. Off-the-shelf parts search | OUT of scope v1 (no local catalog yet; tracked as A1) |
| 5. Plan parameters/labels/datums/bbox | `parameters` + `conventions` + per-step `name` |
| 6. Edit source (parameters-first, gen_step) | per-step `code` lines, parameters-first, closed solids |
| 7. Generate | SLM types `code` into the FreeCAD console (existing executor) |
| 8. Inspect refs (facts/planes/positioning) | **S2 AGENT_STATE** readback (wave-9.1) vs `validation_targets` |
| 9. Snapshot review (multi-view) | **S3 multi-view goal atlas** (already in pipeline) + per-turn screenshot |
| 10. Repair loop (named classes) | per-turn repair hints keyed to the failure classes below |

### text-to-cad conventions baked into the plan prompt

The planner system prompt adopts text-to-cad's defaults verbatim so plans are
consistent and SLM-executable:
- **Units mm; origin at part/assembly center; XY base, +Z up; closed,
  positive-volume solids.** (matches our GOAL_METADATA bbox units.)
- **Parameters-first**: declare all controls before features → enables
  single-number repair and clean ablation.
- **Topology in dependency order**; **booleans on closed operands**; **explicit
  `translate`/`Location` per part** (directly attacks our #1 FC failure —
  stacking at origin).
- **Named labels** per part (also earns our evaluator's name_overlap points).
- Sensible defaults when unspecified: wall 2–3 mm, cosmetic fillet 1–3 mm,
  clearance holes M3/M4/M5 = 3.4/4.5/5.5 mm.

### Failure-class-aware repair (text-to-cad's 9 classes → our S2 hints)

text-to-cad's `repair-loop.md` names 9 failure classes; we already started this
in wave-9 A2 (MISSING_TRANSLATIONS == "positioning mismatch"). Extend the S2
AGENT_STATE reconciliation to emit a named, recipe-bearing hint per class:

| text-to-cad failure class | Our trigger (from S2 AGENT_STATE vs plan) | Injected recipe |
|---|---|---|
| Wrong scale / bounding box | built bbox ≠ `validation_targets.bbox_mm` | "scale off on axis X: built 80 vs target 120 — fix the dim" |
| Missing feature | built `object_count` < plan steps done | "you've built k/N plan parts; emit the next: <step.code>" |
| Positioning / joint mismatch | parts cluster at one center (wave-9 A2) | MISSING_TRANSLATIONS recipe (already shipped) |
| Invalid/missing geometry | S2 reports 0 objects after a build | "code ran but built nothing — check addObject + recompute" |
| Source/syntax | exec_error present | "fix syntax; keep one-line, semicolon-joined" |
| Selector fragility / fillet | (defer — needs addressable refs / build123d) | — |

### Snapshot-review triggers → reuse S3 multi-view

text-to-cad adds extra camera views on risk triggers — "assemblies / >1 body",
"holes on multiple faces or axes", "shells, cavities, bores → section view".
Our S3 multi-view atlas already renders iso+front+top+right and auto-adds a
section view when `bbox_volume/mesh_volume > 1.5`. The planner should *consume*
those views (it gets the same atlas the SLM sees) so the decomposition reflects
top-view tooth/hole counts and section-revealed cavities — the exact cases where
the SLM currently bbox-approximates.

### What we deliberately do NOT port (v1)

Persistent `gen_part.py` source-of-truth, addressable `@cad[#selector]` refs,
build123d DSL adoption, off-the-shelf parts catalog. These are larger lifts
(tracked in `TODO-text-to-cad.md`); the one-pass planner captures the
high-leverage planning skills without them.

## Models

- Small (executor): `qwen/qwen3-vl-30b-a3b-instruct` (unchanged).
- Frontier (planner) candidates via OpenRouter: `anthropic/claude-opus-4-8`,
  `anthropic/claude-sonnet-4-6` (likely best price/quality), an `openai/gpt-*`
  vision model. Make it a `--planner-model` flag; reuse the existing
  OpenRouter client/auth. Planner needs vision (reads the goal image).

## Cost / latency

- ONE planner call per asset: a few k input tokens (image + metadata) + ~1–2k
  output. Order ~$0.01–0.05/asset on Sonnet-class — negligible vs the 6–35 SLM
  calls per trajectory. Latency: one extra ~5–15s call at step 0, amortized.
- Net token cost likely *lower* than status quo if the plan reduces the SLM's
  thrashing (the 35-step loops collapse to ~N plan steps).

## Implementation sketch (when greenlit)

1. New `frontier_planner.py`: `plan(goal_png, metadata, action_spec, model) ->
   BuildPlan` via the OpenRouter client + strict JSON schema. System prompt
   carries the text-to-cad conventions + the brief→params→steps structure.
   Emits per-step `code` (python_eval) AND `op/dims/origin` (for build_* later).
2. `runner.py`: at step 0, call the planner (guarded by `--planner-model`),
   write `build_plan.json`, hold it in memory.
3. `vlm_client.next_action(..., build_plan=..., plan_step=...)`: render a
   `BUILD_PLAN` block (brief + parameters + ordered steps) + "you are on step
   k/N: <step.code>" into the prompt (Variant A). **python_eval-first:** show
   the step's `code` line; the SLM may emit it verbatim or adapt.
4. Plan-progress + failure-class repair in the runner: compare S2 AGENT_STATE to
   `validation_targets` and the current step's expected bbox; advance pointer on
   match, else inject the matching named-failure-class recipe (table above).
5. `--planner-model` flag (+ `--plan-format python_eval|build_star`) plumbed
   through `agent_trajectory.py` → `extra_args` so it's per-job ablatable.

## Evaluation plan

- Benchmark on the SAME 50 wave9d assets, best-of-3, vs the wave-9.1 baseline.
- **Ablation order (revised):** (1) planner-off baseline [= wave-9.1],
  (2) Variant A + `python_eval` plan [DONE: FC +7.0], (3) extend planner to
  Blender + benchmark difficult FC+BL [in progress], (4) best-of-3 to confirm,
  (5) per-step verify-repair (Variant C). `build_*` deprioritized to an optional
  control on the assembly subset, after Variant C.
- Slice by asset class: single-solid (20/32 FC — expect little movement, bbox
  ceiling) vs multi-part (12/32 FC + assemblies — expect the lift, since these
  are the stacking failures the plan targets).
- Primary metric: match_score mean (FC/BL/overall) + the wave-9d failure-mode
  tallies (stacking rate, code-diversity, translate usage, self-terminate rate).
- Track planner cost/latency per asset and plan-vs-built bbox agreement (did the
  SLM actually follow the plan?).

## Risks & mitigations

- **Planner hallucinates dims/positions** → ground it hard on GOAL_METADATA +
  the decompose table (same numbers the SLM is told to trust); validate schema;
  keep the `fallback` field so a bad plan still has a floor.
- **SLM ignores the plan** (it ignored the wave-8 anti-loop directive) → Variant
  B/C reduce reliance on SLM compliance; make the plan step the *primary* prompt
  content, history secondary.
- **Plan-vs-viewport drift** → S2 readback is the reconciliation signal; advance
  only on bbox match.
- **Single-solid assets** (20/32 FC) still bbox-capped — a planner can't make a
  primitive into a toilet; pair with the multi-view/feature goal representation
  tracked separately. Frontier planning mainly helps the multi-part tier.
- **Determinism/replay** → cache `build_plan.json`; planner temperature 0.

## Resolved / remaining questions

Resolved by the user:
- **Variant A first.** ✓
- **`python_eval` downstream first, then `build_*`.** ✓ (one plan, both renderings.)
- **Plan refined from the text-to-cad skills.** ✓ (section above.)

Still open (sensible defaults in parens — will proceed on these unless told otherwise):
1. Planner model (default: `anthropic/claude-sonnet-4-6` via OpenRouter for
   price/quality; bump to Opus only if Sonnet plans underperform).
2. Planner budget — all 50 (default, planner call is cheap + gives a single-solid
   control group) or cap to multi-part assets?
3. If the goal image contradicts GOAL_METADATA bbox, may the planner override it?
   (Default: trust metadata; note the disagreement in the brief.)
