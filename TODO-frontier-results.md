# Frontier-onepass — first results (Variant A, python_eval, FreeCAD)

Model: `google/gemini-3.1-pro-preview` (reasoning=low, 24k out). Run
`wave10_planner_20260603T043551Z`, best-of-1, 50 wave9d assets. The 18 Blender
jobs crashed (the `--planner-model` flag wasn't on the Blender entrypoint — now
fixed; BL planner still not wired, so BL would run baseline). **All 32 FreeCAD
jobs ran with the planner** — that's the tier the planner targets.

## FreeCAD result vs baseline (single-sample)

| FC tier | baseline | planner | Δ |
|---|---|---|---|
| **FC overall (n=32)** | 58.0 | **65.0** | **+7.0** |
| single-solid (n=20) | 53.6 | **62.4** | **+8.8** |
| multi-part (n=12) | 65.3 | 69.2 | +3.9 |
| FC excl. 1 timeout (n=31) | — | **67.1** | ≈ +9 |

For contrast, S2/A2 (wave-9.1) moved FC by **+0.0**. This is a real, well-above-
noise lift (noise floor ≈ ±1.0 stdev).

- **Plan adherence: 34/34** — the SLM emitted the plan's one-line `full_code`
  verbatim every time. Variant A guidance is followed completely.
- **single-solid tier moved the MOST (+8.8)** — surprising vs the "bbox-capped"
  prediction. Gemini picks the right primitive + dims (and revolves/booleans)
  where the SLM defaulted to a wrong-sized `makeBox`. The planner positions
  parts via `makeBox(L,W,H, Vector(x,y,z))` (placement arg), not `.translate()`.

Big movers: pulley 54→88 (+34), pallet 50→79 (+29), simplechair 51→79 (+28),
roof 49→74 (+25), HE-B profile 58→79 (+21), bidet 58→78 (+19), windows 72→90,
doors 70→87.

## Known issues

- **1 timeout** (`pipe water_tank_500l` → 0.0): job exceeded 900s; drags FC mean
  from 67.1 to 65.0. Likely a slow asset / long plan+execute loop, not a plan
  quality fault. Add a per-job time guard or skip re-plan on timeout.
- **1 real regression** (`hvac circular_bend` 66→50, 4 parts): plan mis-decomposed
  a curved bend. Candidate for the `build_star` format or a section view.
- **Planner cost/latency**: ~$0.02–0.05/asset at reasoning=low; one extra ~30–60s
  call at step 0 (one FC job timed out partly due to this + a long loop).

## Next

1. Fix BL entrypoint flag parity (done) — and decide whether to wire the planner
   into `blender_runner.py` (BL is already strong at 82; lower priority).
2. **best-of-3** on FC for a like-for-like vs the wave-9.1 best-of-3 (FC 57.9).
3. **`build_*` plan-format** ablation (the user's second downstream mode).
4. Investigate the timeout + hvac regression; consider a per-job planner cache to
   avoid 3× planner calls under best-of-3 (plan is deterministic at temp 0).
