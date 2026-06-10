# KiCad track — procurement + tuning/ablation results

Status: **M0–M6 done.** KiCad 10.0.3 installed; full pipeline validated on 100
real medium-difficulty boards. Date: 2026-06-10.

See [`SPEC-kicad-pipeline.md`](SPEC-kicad-pipeline.md) for the design and
[`TODO-kicad-pipeline.md`](TODO-kicad-pipeline.md) for the original plan.

## Procurement (100 real boards)

Source: **kicad-happy-testharness** `reference/repo_catalog.json` (5,856 indexed
repos with a per-repo `complexity` block). Selected the **medium band** =
`total_components` ∈ [44, 203] (catalog p40–p75), modern (KiCad-6+ file
version), ≥1 pcb file; seeded-shuffled to span the band; cloned each, extracted
+ pcbnew-validated the largest board, staged 100. Tool: `procure_kicad_assets.py
--catalog`.

Staged set: **100 boards**, components min 44 / median 95 / max 203, copper
layers 2–6. Manifest: `~/cua_kicad_smoketest/manifest.jsonl`.

## M5 — planner ablation (cheap models suffice)

12 boards × 3 planners, **headless** (plan → reconstruct `full_code` onto the
seed → `compare_kicad`; the GUI replay is planner-agnostic, so this isolates
plan quality cheaply). Tool: `kicad_plan_ablation.py`.

| planner | n | mean match_score | std | out-tokens |
|---|---|---|---|---|
| **gemini-3-flash** | 12 | **27.5** | 26.3 | 4092 |
| gemini-3.1-flash-lite | 10 | 23.0 | 22.0 | 4020 |
| gemini-3.1-pro | 12 | **27.5** | 26.3 | 3978 |

**flash and pro are statistically identical** (same mean + std; 9/12 boards
all-tie); near-identical token cost; flash-lite dropped 2 calls to API errors.
→ confirms the plan's "Flash ≥ Pro / cheap suffices" hypothesis on real boards.
**Locked default: `google/gemini-3-flash-preview`** (`build_jobs_default.py`).

## M4 — metric analysis (what the score actually measures)

`compare_kicad` is correct, but on real boards `match_score` correlates
**r = +0.97 with footprint availability** — i.e. it is dominated by an
*environment* constraint, not planner quality (which is *why* all planners tie).
Two ceilings:

1. **Footprint availability.** Mean `agent_fp/goal_fp` = **0.37** (after adding a
   system-wide footprint fallback that loads a standard-named footprint from any
   installed `.pretty` lib; was 0.30 without it). 7/12 boards have <50% of their
   footprints in the system library — the rest are custom/project footprints
   that ship inside the source repo and can't be reconstructed from libraries.
2. **pcbnew loader crashes.** A minority of boards (e.g. `ECE395`, `labamp`)
   **segfault pcbnew's SWIG `FootprintLoad`** on a specific footprint — the board
   is unreconstructable regardless of planner. Handled safely: reconstruct is a
   subprocess, so a crash → no stats → score 0.

Component means (flash, with fallback): placement 0.37, **nets 0.000**, outline
0.58, counts 0.31. **nets = 0 everywhere** — the grounded `full_code` places
footprints + board outline but does not reconstruct connectivity (routing is the
deferred hard part per the spec), so the nets weight (25) is an inherent ~25-pt
ceiling in the current placement regime.

**Metric verdict:** sound as an absolute reconstruction-fidelity score; for
*comparing planners* it is availability-limited, but the comparison conclusion
(flash ≈ pro) is robust because all planners hit the same ceiling. Real-world
takeaway for the pipeline: prefer boards whose footprints are mostly standard
(half-adder, esphome-style) and/or install more footprint libraries; treat
connectivity reconstruction as future work.

Critical fix found during M4: the planner's one-line `full_code` rule forced
`def`-cramming → SyntaxError → 0 footprints. Switched to **multi-line `full_code`
with a resilient `P()` helper** (code is exec'd from a file in both the GUI
executor and the headless reconstructor). On a 62-footprint board this moved the
score 2.0 → 60.3.

## M6 — GUI trajectory collection (scale demo)

Full compositional GUI pipeline (`kicad_collect.py` → `kicad_agent_trajectory.py`)
on the most-reconstructable boards: plan → console replay under Xvfb →
`trajectory.mp4` + clean video → `evaluate_kicad_run`.

4 boards, flash planner, compositional. **4/4 produced `trajectory.mp4` + clean
video; mean match_score 47.8.**

| board | components | GUI score | headless score | footprints (GUI) |
|---|---|---|---|---|
| esphome-dot | 146 | 60.3 | 60.3 | 56/62 |
| FunBox | 200 | 58.9 | 58.9 | 58/70 |
| iris-hardware | 53 | 45.7 | 45.3 | 29/54 |
| half-adder-kicad | 54 | 26.1 | 66.6 | **7/70** |

3/4 match their headless scores closely — i.e. **the GUI compositional console
replay places the same footprints as the direct headless build**, end-to-end
under Xvfb, with video. The outlier is `half-adder`: its compositional
**decompose** (Stage-2 split of `full_code` into per-step console blocks)
abbreviated the 70-footprint build into 4 steps and only 7 footprints survived,
vs 70/70 when the `full_code` is run directly. So the compositional *decompose*
is **lossy on high-footprint boards** — a `_DECOMPOSE_KICAD` prompt-tuning item
(allow more steps / forbid abbreviating the P() list), separable from the core
pipeline which works. Artifacts: `~/cua_kicad_smoketest/runs/m6/<board>/`
(`trajectory.mp4`, `video_clean.mp4`, `build_plan.json`, `eval/eval.json`).

## Full 100-board collection (4 workers)

Ran all 100 staged boards through the parallel orchestrator
(`parallel_orchestrator.py --app kicad --num-workers 4 --display-base 200`),
flash planner, compositional off-screen-console build + clean videos. Report:
`kicad_run_report.py` (`run_report.json`).

**Status:** 100/100 succeeded (`terminated_by=agent`), 0 failed / 0 timed-out /
0 loop-killed. **100/100 clean videos produced.**

**Performance** (match_score 0–100): mean **31.0**, median **27.6**, max **66.7**.
Distribution is **bimodal** — 41 boards in [50,70) (standard footprints,
reconstructable) and 42 in [0,10) (custom/project footprints not in the system
library, or pcbnew loader crashes). 46/100 boards reconstruct cleanly
(agent_fp/goal_fp ≥ 0.5); mean footprint availability 0.43. Top boards
(Octuplex 35/35, LabPowerSupply 53/53, ECG-Sensor 49/49) hit 66.7 — the ~67
ceiling is the connectivity component (nets weight 25 = 0, no routing in the
placement regime). This is the M4 footprint-availability ceiling at scale, not a
planner-quality issue.

**Speed:** wall time **66.5 min** for 100 boards on 4 workers (8-core box);
**~40 s/board** amortized, **1.50 boards/min**; ~160 worker-seconds/board
(per-job mean 142 s, median 143 s). 4 workers was the right level for 8 cores
(load ≈ 7/8 under build, software-rendered pcbnew + ffmpeg).

**Cost: $1.79 total, $0.018/board** (gemini-3-flash planner only — compositional
mode never calls the VLM executor). 831k prompt tokens ($0.42) + 458k completion
tokens ($1.37) + 100 goal images. The completion cost dominates because the
`full_code` lists one `P(...)` call per footprint; skipping the (failed) LLM
decompose halved the per-board planner calls. ~1.8¢/board is in the cheap regime
(a bit above FreeCAD's ~½¢ because PCBs have far more components per asset).

## Open items (next)

- **Decompose fidelity** on large boards (half-adder 70→7): tighten
  `_DECOMPOSE_KICAD` so every P() call is preserved across steps; or replay
  `full_code` directly when footprint count is high and only decompose the
  *visual* grouping.
- **Footprint availability**: install more KiCad libraries and/or prefer boards
  with standard footprints; a minority of boards crash the pcbnew loader.
- **Connectivity**: teach the planner to add nets + route (lifts the nets=0
  ceiling); routing is the deferred hard part.

## Tooling added this pass

- `procure_kicad_assets.py --catalog` — catalog-driven medium-board sourcing.
- `kicad_plan_ablation.py` — headless planner A/B (parallelized).
- `kicad_metric_report.py` — ablation / metric analysis.
- `kicad_collect.py` — M6 batch GUI collector.
- planner `full_code` → multi-line `P()` helper; `kicad_reconstruct.py`
  system-wide footprint fallback.
