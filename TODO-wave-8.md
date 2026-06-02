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

## Results — Wave-8 (partial, 25/47 scoreable) vs W6 baseline

Benchmark in progress (mostly FC done; BL still running). Slow because
max_steps trajectories now run all 35 turns instead of dying at step 3-4.

### Structural change (definitive even from partial data)

| Termination type | W6 (n=83) | Wave-8 partial (n=25) |
|---|---|---|
| `agent_loop_detected` | **70 (84%)** | **0 (0%)** |
| `agent` (clean term) | 13 (16%) | 11 (44%) |
| `max_steps` | 0 (0%) | 14 (56%) |

**Loop-kill is gone.** Synthetic frame_view injection successfully breaks
the loop-detector's identical-sequence count. Clean termination tripled.
Most trajectories that would have been loop-killed now run to max_steps
instead.

### FC clean-termination jump

| App | W6 | Wave-8 (partial) |
|---|---|---|
| FreeCAD | **0/44 (0%)** | **5/8 (62%)** clean term |
| Blender | 13/39 (33%) | 6/17 (35%) clean term |

FC's structural termination problem fundamentally fixed. The
`Gui.SendMsgToActiveView('ViewFit')` append (item A) + threshold bump
(item 1) + synthetic injection (item 2) combination works.

### Score impact (partial — only 25/47 comparable)

| | n | W6 baseline | W8 partial | Δ |
|---|---|---|---|---|
| Overall | 25 | 56.7 | 57.8 | **+1.1** |
| Blender | 3 | 85.3 | 77.2 | -8.1 (small n) |
| FreeCAD | 22 | 52.8 | 55.2 | **+2.4** |

FC lift (+2.4) is in line with W7.1's +2.2. **Structural fix didn't
translate into a major score lift** — exactly as the smoke predicted.
The agent still emits identical code repeatedly within the longer
trajectories; loop-kill removal gives it more attempts but they're all
the same attempts.

### Why scores barely moved

Sample from W8 traj `fc__generic__cup_step` (max_steps, 35 turns):
agent emitted IDENTICAL `python_eval` 35 times in a row. Hash check
confirms — same code 35x. The anti-loop directive (item 3) was injected
from turn 3+ but the agent ignored it.

Sample from W8 traj `fc__chain__simplex_½x⅛` (clean term, 6 turns):
agent emitted IDENTICAL code 5 times, then on 6th turn finally
terminated. **Threshold bump 3→5 was the win here** — the agent did
eventually notice. Without the bump, it would have died at retry 3.

### Verdict (from partial)

Wave-8 is **necessary but not sufficient**. It eliminates the
loop-kill failure mode (categorical structural change), gives the
agent runway to iterate, but doesn't move scores meaningfully because
agent behavior on the same code stays the same.

The bottleneck moved from "loop-kill terminates the run prematurely"
to "agent emits the same wrong code N times even with more chances".

**Wave-9 (S2 post-build state + A2 repair recipes) is the obvious
next intervention** — gives the agent factual signal that its code
already built X objects with Y dimensions, so it can pivot from
"retry" to "change code" decisively.

### Final results (to be filled in when full benchmark completes)

- Wall time projection: ~5h total (started 04:37 UTC, 15/50 at 85min mark)
- Final Overall mean: ?
- Final BL mean: ?
- Final FC mean: ?
- Final clean-termination rate FC: ?
