# TODO — wave-8 branch

Off `text-to-cad` branch at `50cd18d`. Wave-8 ships the three additional
fixes that items A+B in `text-to-cad` exposed as necessary:

## Shipped in this branch

| File | Change | LOC |
|---|---|---|
| `runner.py` | bump `loop_kill_repeats` default 3 → 5 | 1 |
| `runner.py` | inject synthetic `{type: frame_view, _auto: true}` step after every successful python_eval | ~12 |
| `runner.py` | prepend ANTI-LOOP DIRECTIVE to hist_hint when last 2 evals are identical | ~20 |
| `blender_runner.py` | mirror all three changes | ~33 |

Plus inherited from `text-to-cad`:

| File | Change | LOC |
|---|---|---|
| `action_space.py::_do_python_eval` | append `;Gui.SendMsgToActiveView('ViewFit')` + click viewport + iso/fit keys | ~25 |

## Smoke result (usb-micro-b, max_steps=8)

**Mechanism confirmation:**
- Loop-kill correctly avoided — `terminated_by=max_steps` instead of `agent_loop_detected`
- All 8 agent turns produced [py, auto-frame, py, auto-frame, ...] in trajectory record
- Anti-loop directive prepended from turn 3 onward (rationales now reference "previous attempts used the same code repeatedly")
- Score 51.5 vs W6 baseline ~32 on the same asset class — modest lift

**What the smoke revealed (next-layer problem):**

The agent now SEES the directive, ACKNOWLEDGES in rationale that prior
attempts used identical code, but STILL emits the same code an 8th time.
Sample step 11 rationale:

> "The goal is a compound object with 14 parts, and the previous attempts
>  used the same code repeatedly without visible change. The current state
>  shows a single box, so [I will emit the same code again]"

The agent's failure mode is:
```
"build doesn't match goal" → "build didn't take effect" → retry same code
```

The correct mental model would be:
```
"build doesn't match goal" → "code is wrong" → change parameters
```

This is a fundamentally different problem than the viewport-staleness loop.
The agent has the right awareness ("previous attempts used the same code")
but the wrong action selection ("emit the same code").

Specifically, on this usb-micro-b job the agent emitted 14
`Part.makeBox(...)` calls all at the world origin (no `translate()`), so
all 14 boxes stack at (0,0,0) → visually appears as 1 box → agent
infers "code only built 1" → retries.

## Next-layer fixes (NOT in this branch)

| # | Item | Cost | Expected | Why |
|---|---|---|---|---|
| 1 | **S2 post-build state injection** — after each python_eval, extract `App.ActiveDocument()` object count + bbox and inject as `AGENT_STATE: {n_objects: 14, bbox: [...], ...}` | ~300 LOC | +5-8 | Closes the "build succeeded" loop: agent sees "I built 14 objects, all at origin" and knows to add `.translate()` |
| 2 | **A2 repair loop with named failure classes** — when AGENT_STATE shows "objects built but all overlap at origin" or "wrong bbox", inject a named recipe ("MISSING_TRANSLATIONS: 14 objects all at (0,0,0). Add .translate(Vector(x,y,z)) to each before adding to doc.") | ~100 LOC | +3-5 | Codifies the "change code, don't retry" intuition into explicit named fixes |
| 3 | **Decompose prompt fix** — current `_W4_DECOMPOSE_BLOCK` lists per-part bbox+origin but the per-part origins are ABSOLUTE world coordinates from the goal decomposer. Agent reads them but doesn't always use `.translate(Vector(*origin))`. Emphasize this in the template, maybe with a worked example. | ~30 LOC | +2-3 | Targeted fix for the multi-part-stacked-at-origin failure |

The S2 + A2 combo would directly address what Wave-8 smoke surfaced.
The decompose-prompt fix is the cheap immediate win.

## Benchmark plan for Wave-8

Same 50 W6 jobs, 4 workers, max-steps 35, job_timeout_sec 900.
Compare to W6 baseline AND W7.1 (gated S3) to measure:

- **Loop-kill rate**: should drop dramatically from W6's 84% (synthetic
  injection breaks detection)
- **Clean-termination rate**: W6 was BL 33% / FC 0%. Predict W8 BL stays
  ~33%, FC moves from 0% to ~10-20% (still bottlenecked by code-correction
  issue, but some terminations should land where agent gets lucky)
- **Mean match_score**: marginal change expected. W6 overall 69.3,
  W7.1 71.4. Predict W8 ~71-74 — the agent gets more time to iterate
  but isn't iterating productively. The big lift waits for S2+A2.

## Wave-9 scope (planned, not started)

- **S2 post-build state injection** — primary intervention identified by Wave-8 smoke
- **A2 repair-loop recipes** — paired with S2
- **Decompose-prompt translate emphasis** — cheap immediate win

Each of these would warrant its own branch + benchmark.

## Results — Wave-8 vs W6 baseline + Wave-7.1

(to be filled in after benchmark completes)

- Loop-kill rate: W6 = 84%, W8 = ?
- BL clean term rate: W6 = 33%, W8 = ?
- FC clean term rate: W6 = 0%, W8 = ?
- Overall mean: W6 = 69.3, W7.1 = 71.4, W8 = ?
- BL mean: W6 = 83.8, W7.1 = 85.7, W8 = ?
- FC mean: W6 = 52.8, W7.1 = 55.0, W8 = ?
