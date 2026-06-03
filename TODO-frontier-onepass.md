# Scoping — One-Pass Frontier-Planner Approach

**Branch:** `frontier-onepass` (isolated worktree; main tree stays on `wave-9`
for the running benchmark). This is a SCOPE/design doc — nothing implemented yet.

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

**Output schema (recommend STRUCTURED, validated):**
```json
{
  "summary": "EUR-1 pallet: 3 deck boards on 9 blocks on 3 stringers",
  "shape_class": "assembly | single_solid | revolve | boolean",
  "steps": [
    {"i": 1, "op": "build_box", "dims": [1200,100,22], "origin": [0,0,122],
     "name": "deck_top", "why": "top deck board"},
    {"i": 2, "op": "build_box", "dims": [...], "origin": [...], "name": "...", "why": "..."},
    {"i": 7, "op": "compound", "shapes": ["deck_top","..."], "name": "pallet"}
  ],
  "fallback": "if assembly is too complex, a single 1200x800x144 box scores ~50"
}
```
Structured output is validated (retry on mismatch), maps 1:1 onto the SLM's
action space, and lets the runner track plan progress mechanically.

Alternatives considered: (a) free-text NL plan — easy but not machine-trackable;
(b) a single ready-to-run python_eval reconstruction — see Variant B.

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

Recommend implementing **A first** (smallest change, isolates the planning
benefit), then **C** if A shows lift on multi-part assets.

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
   BuildPlan` using the OpenRouter client + a strict JSON schema.
2. `runner.py`: at step 0, call the planner (guarded by `--planner-model`),
   write `build_plan.json`, hold it in memory.
3. `vlm_client.next_action(..., build_plan=..., plan_step=...)`: render a
   `BUILD_PLAN` block + "you are on step k/N: <step>" into the prompt (Variant A).
4. Plan-progress tracking in the runner: compare S2 AGENT_STATE bboxes to the
   current step's expected bbox; advance pointer on match, inject a targeted
   repair hint on mismatch.
5. `agent_trajectory.py` + orchestrator jobs-file: add `--planner-model` to
   `extra_args` so it's a per-job toggle (ablatable in the benchmark).

## Evaluation plan

- Benchmark on the SAME 50 wave9d assets, best-of-3, vs the wave-9.1 baseline.
- Ablate: A vs B vs C; planner-on vs planner-off; per asset class
  (single-solid vs multi-part — expect the biggest lift on multi-part, which
  the SLM stacks today).
- Primary metric: match_score mean (FC/BL/overall) + the wave-9d failure-mode
  tallies (stacking rate, code-diversity, translate usage, self-terminate rate).

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

## Open questions (for the user)

1. Variant to build first — A (guidance, recommended), B (script), or C (hybrid)?
2. Planner budget: cap to multi-part assets only (where the win is), or all 50?
3. Frontier model preference (Sonnet-class default for cost) — any constraint?
4. Should the plan be allowed to emit raw `python_eval` (full expressivity, incl.
   revolves/booleans for organic shapes) or be restricted to the structured
   build_* primitives (safer, trackable)?
