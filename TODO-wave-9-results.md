# Wave-9 Results — S2/A2 + loop_killed (vs Wave-9d baseline)

Same 50 assets as wave9d (`runs/wave9_20260602T194856Z` vs
`runs/wave9d_20260602T160657Z`). Code = `wave-9` branch.

## Scores — FLAT (within noise)

| | Wave-9d | Wave-9 | Δ |
|---|---|---|---|
| FreeCAD | 58.0 | 57.3 | −0.7 |
| Blender | 82.2 | 82.3 | +0.1 |
| **Overall** | **66.7** | **66.3** | **−0.4** |

FC by decomposition: multi-part (n=12) 65.3→65.2; single-solid (n=20) 53.6→52.6.

## The −0.4 is noise, not signal

Run-to-run |Δ| with **single sample per job**:
- Blender (NO code change applied at all): mean |Δ| = **1.8**, max **16.5**
- FreeCAD (S2/A2 applied): mean |Δ| = 1.7, max 18.4

BL swings ±1.8 avg / ±16 tail with zero code change. The FC aggregate Δ (−0.7)
is well inside that band. **n=1 cannot resolve a few-point fix.** Individual
"movers" (pyramid +16.5, tower −13.2, pulley −18.4) are mostly sampling/
framing/eval-render variance, not the fix.

## What DID work

1. **loop_killed reporting fix — clear win.** Result is now
   `38 succeeded + 11 loop_killed (valid builds) = 49/50`, vs the old
   misleading `41 succeeded / 8 failed`. Loop-killed jobs exit 0, are exempt
   from retry, and are no longer counted as failures.

2. **Decompose-prompt + A2 changed behavior as intended:**
   - `.translate()` usage: **3/33 → 12/33** FC trajectories (4×).
   - code-diversity (distinct/total evals): 0.14 → **0.22** (agent repeats less,
     adapts more).
   - `mit_e_vent` +2.7, `doors_metal_simple` +7.6, `smd_c_0402` +3.1 — the
     decomposed/multi-part jobs that the fix targets.

   The behavior moved in the right direction; the score didn't follow because
   applying translate to the *correct* positions is still hard, and the 20
   single-solid FC assets are bbox-capped (a primitive can't be a toilet).

## BUG found: S2 probe fires in only 14/33 FC jobs

`_state_probe.py` written in all 33 FC dirs, but `agent_state.json` produced in
only **14**. Diagnosis: jobs with successful builds (e.g. `smd_c_0402`: 35
clean python_evals, 0 exec_errors) still wrote no state JSON. The probe is
appended to the SAME typed console line as the build code; the resulting line
(build + ViewFit + `exec(open(r'<126-char abs path>').read())`) is long enough
that FreeCAD console autocomplete/timing corrupts the `exec(...)` tail, so it
never runs. The build half succeeds (frame_view injected), the probe half
silently fails. So S2 was only ~42% deployed → the experiment is under-powered.

## Wave-9.1 update — S2 fixed + best-of-3 (the definitive read)

S2 probe fix (separate short-path console submission) verified: `agent_state.json`
now in **96/96 FC trials** (was 14/33). Re-benchmarked best-of-3 (150 trials) on
the same 50 assets. All 150 produced valid builds (110 succeeded + 40 loop_killed).

| | best-of-3 | mean-of-3 |
|---|---|---|
| FreeCAD | 57.9 | 57.8 |
| Blender | 83.5 | 81.4 |
| **Overall** | **67.1** | **66.3** |

Per-job noise floor (stdev across 3 trials): **mean 1.0**, max 16.8 — most jobs
are reproducible; only a few swing.

**Verdict: even with S2 100% deployed and properly multi-sampled, S2/A2 does NOT
move match_score.** mean-of-3 = 66.3, identical to wave-9 (66.3) and wave-9d
(66.7). best-of-3 = 67.1 (+0.8 ceiling from 3 draws). The behavior changed
(translate 3→12, code-diversity up) but the 30B model's *planning ceiling* is
the wall: better post-build feedback doesn't grant better decomposition.

**This is the empirical case for the frontier-planner approach** (`frontier-onepass`
branch): the bottleneck is planning capability, not feedback — so put planning in
a frontier model and hand the 30B an authoritative plan. Keep the loop_killed
reporting fix (clear win); treat S2 as plumbing the planner will leverage (its
AGENT_STATE readback is the plan-vs-built reconciliation signal).

## Next (Wave-9.1)

1. **Fix S2 reliability** — execute the probe as a SEPARATE console submission
   (type build → Enter → type probe → Enter), or use a short fixed path
   (`/tmp/_cua_state_<worker>.json`) to shorten the line. Re-verify
   agent_state.json present in ~all FC dirs.
2. **Multi-sample the benchmark** — best-of-3 (or 3 repeats) per job so a
   few-point effect clears the ±1.8 noise floor. Single-sample runs cannot
   evaluate fixes of this magnitude.
3. Only THEN judge S2/A2 on score. Behavior signals (translate 4×) say the
   mechanism works; we need it fully deployed + multi-sampled to measure lift.
4. Single-solid FC (20/32) remains out of reach for decomposition/A2 — needs a
   richer goal representation (multi-view / per-feature), tracked separately.
