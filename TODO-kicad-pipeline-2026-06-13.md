# KiCad pipeline — full investigation + state, 2026-06-12/13

Picks up after the 100-board collection landed (TODO-kicad-collection.md). This
file is the source of truth for what we know, what we shipped, and where to dig
next.

## 50-board ablation set
- `~/cua_kicad_smoketest/manifest.jsonl` — the 50 staged boards.
- `~/cua_kicad_smoketest/assets/<board>.kicad_pcb` — board files.
- `~/cua_kicad_smoketest/screenshots/<stem>.png` — goal atlas.
- `~/cua_kicad_smoketest/screenshots/<stem>.meta.json` — goal-metadata sidecar
  (`kicad.footprints[]`, `net_count`, `layer_count`, `outline_bbox_mm`).

Baselines that exist:
| file | model | reasoning | notes |
|---|---|---|---|
| `fix1_results.jsonl` | flash@low | low | OLD baseline (50-board set used a slightly DIFFERENT manifest — only 25/50 overlap with current). DON'T compare directly. |
| `flashlow_baseline.jsonl` | flash@low | low | CURRENT 50-board flash baseline. **Mean 42.3. Median 37.8. p25=0. p75=84.9.** |
| `pro_baseline_results.jsonl` | pro@low | low | Pro on current 50-board set. Mean 46.8 (n=48; 2 schema-validation fails on enormous panel boards). |
| `decomp_results.jsonl` | flash@low | low | flash@low + SUBCIRCUIT CLUSTERS block in prompt. Δmean +0.00, 0/49 boards moved — confirmed inert. |
| `modelscan_*` (4 files) | flash@med / pro@low × decomp on/off | varies | The 4-cell sweep that confirmed decomp inert at every tier. |
| `lib_rescue_flash.jsonl` | flash@low + project libs + extra libs | low | The first lib-rescue attempt. Same as flash baseline (0/22 rescued) — see "Bug 1" below. |
| `lib_rescue_v2.jsonl` | flash@low + project libs (after partial fix) | low | 1/22 rescued (ecad-viewer 0→19.9). |
| `lib_rescue_shimonly.jsonl` | flash@low + NO project libs (final shim fix) | low | **2/22 rescued (NerdNOS 0→94.3, ecad-viewer 0→19.9).** This is the current production rescue. |

## Score distribution (flash@low current baseline)

```
[  0-  9]  22  ######################   <- "broken cluster"
[ 10- 19]   0
[ 20- 29]   1
[ 30- 39]   3
[ 40- 49]   0
[ 50- 59]   2
[ 60- 69]   3
[ 70- 79]   6
[ 80- 89]   4
[ 90-100]   9
```
Mean 42.3, median 37.8. Strongly bimodal: 22 boards stuck at 0 + a working
shoulder from 50–99.

## What we shipped (in commit order)

| Commit | What | Impact (paired on common boards) |
|---|---|---|
| `1828e81` | Fix #1: nets in plan + N() helper in planner template | **+14.25** (universal) |
| `1828e81` | Fix #2: SparkFun .pretty libs registered in reconstruct | +0.13 (sub-additive but unblocks #3) |
| `94c1cb9` | Fix #3: `KIRECON_PROJECT_LIBS_ROOT` scans cloned source repos | +1.25 overall, **+27.6 on Kicad_Library** |
| `628028c` | Fix #4: panelization detect | **REJECTED** — broke urchin; reverted |
| `1828e81` | Fix #5: persist agent_state per compositional batch (diagnostic) | n/a |
| `d5773c5` | KiCad decomposition (anchor-based subcircuit clustering) | +0.00 / 0 boards moved — **inert**, shipped as infrastructure (manifests at `<assets_parent>/decomposed/<stem>/manifest.json`) |
| `f66a45b` | Decomp default OFF in planner prompt + `json_repair` fallback in `_extract_json` | json_repair: **0/50 parse failures** vs prior 13–30%. Decomp: confirmed inert at flash@low + flash@medium + pro@low (paired Δmean 0.00 at every tier). |
| `af1ddf5` | Hybrid planner router for KiCad (`fp>=35 AND nets>=60` → pro@low) | **+4.14 mean** (10 boards up / 0 down on n=48 paired). Mirrors `_freecad_should_escalate`. Wired in `planner_router._kicad_should_escalate`, `kicad_runner.planner_escalate_model`, CLI `--planner-escalate-model`, `build_jobs_default.KICAD_PLANNER_ESCALATE`. |
| `509cd5f` | **Shim name-fallback REMOVED** from `kicad_reconstruct.py` + lib-rescue tooling | **+1.9 mean** (NerdNOS 0→94.3). The big finding — see Bug 1 below. |
| `a9aa7a6` | **Planner template `SetLayerAndFlip` → `SetLayer`** (one line, P() helper) | **+36.57 mean** (22/22 broken-cluster boards rescued). The DOMINANT fix of the day — see Bug 2 below. |

Cumulative production gain on the current 50-board set: ~**+42.6 mean** above
the pre-hybrid flash@low baseline (42.29 → 78.86 from these fixes alone, before
re-applying the hybrid router on top).

## Hybrid router (af1ddf5) — the rule

Pareto-best on the 50-board paired sweep:

```
escalate to pro@low when fp_count >= 35 AND net_count >= 60
```

Catches all 10 boards pro@low improves (full +4.14 paired-mean lift), routes
29/48 boards (60%), saves ~40% of pro budget vs always-pro. Where pro NEVER
helps: the broken cluster (flash<30, planner-fails — pro can't rescue) and the
saturated cluster (flash>=85, already near-perfect).

| | flash-only | always-pro | **hybrid (shipped)** |
|---|---|---|---|
| Predicted mean | 42.6 | 46.8 | **46.8** |
| Pro calls / 50 boards | 0 | 50 | **29** |
| Est. cost / 50 boards | $0.08 | $5.80 | **$3.17** |

## Bug 1: shim name-fallback was silently SIGSEGV'ing reconstruct

This is the headline finding. Spent hours assuming the broken cluster was a
library-availability problem. It was not.

`scripts/kicad_reconstruct.py`'s `_safe_fpl` (from fix #2/#3) had three layers:
1. try the lib path the caller asked for verbatim,
2. nickname-remap (same nickname in any registered root),
3. **name-fallback** — index ALL `.kicad_mod` files across ALL registered
   `.pretty` dirs (~16,800 entries) and search by footprint name.

The third layer caused a non-deterministic SIGSEGV inside pcbnew's SWIG bindings
during chunk replay. Reconstruct crashed with rc=139, **no stats file written**,
every broken-cluster board reported `matched=0/N` regardless of how many of its
footprints were actually std-lib loadable. That masking made every prior
library-rescue effort look like a no-op.

Repro on NerdNOS:
- with name-fallback present: reconstruct rc=139, 0 fp placed.
- with name-fallback removed: reconstruct rc=0, 42/48 fp placed, score 0 → 94.3.

Also added: skip legacy `(module`-format `.pretty` dirs at registration (SWIG
asserts/segfaults on those). Some MODERN `(footprint`-format dirs (e.g.
NerdNOS's bitaxe.pretty — 41 mods, version 20221018) ALSO segfault on load and
we can't cheaply detect them: subprocess probing crashes the parent
(pcbnew/wxApp state inherited across fork() is broken). The 4 bitaxe-only
footprints in NerdNOS still fail to load — that's the 42/48 gap, acceptable.

## Bug 2: planner emits chunks that crash pcbnew (FIXED — commit a9aa7a6)

After Bug 1 was fixed, 20/22 broken-cluster boards still scored 0. They failed
in a DIFFERENT layer: the planner's emitted code itself crashed pcbnew, even
when exec'd directly (no shim).

Bisected on labamp's chunk:

| chunk variant | pcbnew exec result |
|---|---|
| unmodified | SIGSEGV (no fp placed) |
| no `pcbnew.Refresh()` | SIGSEGV |
| `SetLayerAndFlip` removed | OK, 35 fp placed |
| `fp.Flip(fp.GetPosition(), False)` | SIGSEGV (alternate flip API also crashes) |
| `fp.SetLayer(B.Cu)` | OK, 35 fp placed, 7 on back |

The single line `fp.SetLayerAndFlip(b.GetLayerID('B.Cu'))` in the P() helper
was the killer. SetLayer alone places the footprint on the B.Cu layer without
geometrically mirroring the pads — partial layer fidelity loss but no crash.
Patch: one line in `_PLANNER_SYSTEM_KICAD`.

### Impact (50-board ablation set)

| metric | before Bug 2 fix | after | Δ |
|---|---|---|---|
| mean | 42.29 | **78.86** | **+36.57** |
| median | 37.75 | 85.90 | +48.15 |
| boards in [0,9] | 22 | **0** | -22 |
| boards in [90,100] | 9 | **23** | +14 |

22/22 broken-cluster boards rescued (>1pt). 20/22 above 50. 14/22 above 90.

| board | flash baseline | after Bug 2 fix |
|---|---|---|
| bms_hw_voltmod | 0.0 | **100.0** |
| Controller-Drone | 0.0 | **99.0** |
| subsystems | 0.0 | **98.5** |
| labamp | 0.0 | **97.5** |
| Coilchain-Hardware | 0.0 | 96.7 |
| Avionic-Mastodonte | 0.0 | 96.5 |
| OtterPill | 0.0 | 95.9 |
| Laser_Backplane_DVI | 0.0 | 95.2 |
| NerdNOS | 0.0 | 94.3 |
| Igloo64 | 0.0 | 93.7 |
| ECE395 | 0.0 | 93.4 |
| teka_hardware | 0.0 | 92.7 |
| kicad-testing-station | 0.0 | 92.4 |
| KiCad_TopiBadge | 0.0 | 90.0 |
| pygmy | 0.0 | 80.1 |
| RTB_D98 | 0.0 | 79.8 |
| jsmartsw_hw | 0.0 | 76.5 |
| RFSWITCH01 | 0.0 | 68.9 |
| ESP32S3-DEVKIT-MINI | 0.0 | 68.4 |
| ISM01 | 0.0 | 56.7 |
| analog-toolkit | 0.0 | 42.3 |
| ecad-viewer | 0.0 | 19.9 |

Results saved at `~/cua_kicad_smoketest/lib_rescue_bug2fix.jsonl`.

### Next: validate pro@low + hybrid router under Bug 2 fix

`pro_baseline_results.jsonl` was produced BEFORE the Bug 2 fix landed, so the
hybrid router's +4.14 estimate is now stale. Pro's broken-cluster boards
should also lift (likely to ~90+ levels) with the template fix. Need to
re-run `kicad_pro_baseline.py` to get a clean pro@low baseline, then re-tune
the hybrid threshold if needed (the `fp>=35 AND nets>=60` rule was selected
when broken-cluster boards were all 0; the math changes now).

Open question: with broken-cluster boards now near-perfect under flash@low,
is there ANY pro@low headroom left worth the 60× cost? The pre-fix gain
was driven by boards in the [50,84] band where pro added a clean +5–6pt.
Those boards are still candidates, but the broken cluster is no longer in
need of rescue from pro.

## Lib-rescue infrastructure (shipped, but didn't help for Bug 1 reasons)

| File | What |
|---|---|
| `scripts/kicad_clone_project_libs.py` | Clone any board's source repo into `~/kicad-project-libs/<owner>__<name>` for `KIRECON_PROJECT_LIBS_ROOT`. |
| `scripts/kicad_clone_extra_libs.py` | Clone third-party libs referenced by 2+ broken boards: Jan-Henrik-KiCAD-libs (otter.pretty, legacy `(module` — gets filtered), Digi-Key/digikey-kicad-library (digikey-footprints.pretty, also legacy — also filtered). MLAB couldn't be located (MLAB-project/Kicad-libraries doesn't exist). Useful catalogue regardless. |
| `scripts/kicad_lib_rescue_ablation.py` | Per-board project-libs setup + planner+eval chain. `--no-project-libs` measures shim fix in isolation. |

22 source repos already cloned in `~/kicad-project-libs/`. Don't re-clone unless
you nuke that dir.

## Per-board library demand profile (22 broken boards)

```
ecad-viewer       189 fp missing  "footprints"          (project lib)
ISM01              94 fp missing  Mlab_R + Mlab_Pin_Headers + Mlab_Mechanical + Mlab_D + Mlab_CON
analog-toolkit     64 fp missing  otter                 (Jan-Henrik-KiCAD-libs)
RFSWITCH01         31 fp missing  Mlab_R + Mlab_Pin_Headers + Mlab_Mechanical + Mlab_D + MLAB_SAW
RTB_D98            24 fp missing  Custom_Parts + RTB    (project libs)
ESP32S3-DEVKIT     21 fp missing  PCM_4ms_Resistor + digikey-footprints + PCM_Package_TO_SOT_SMD_AKL
jsmartsw_hw        13 fp missing  jSmartSW              (project lib)
teka_hardware      12 fp missing  PCM_Resistor_SMD_AKL + footprints
kicad-testing-st   11 fp missing  external_footprints   (project lib)
KiCad_TopiBadge     8 fp missing  digikey-footprints + user-footprints
NerdNOS             6 fp missing  bitaxe                (project lib, MODERN format, but segfaults on load)
pygmy               6 fp missing  LC86G + Logos + lsm6dso32 + LPO3310-222MLC + microsd (long tail)
Laser_Backplane_DVI 5 fp missing  custom                (project lib)
Coilchain-Hardware  5 fp missing  coilchain-switch      (project lib)
Avionic-Mastodonte  4 fp missing  Library + RP-Pico Libraries + NO-LIB
OtterPill           4 fp missing  otter                 (Jan-Henrik-KiCAD-libs)
Igloo64             3 fp missing  Igloo                 (project lib)
ECE395              1 fp missing  ECE395                (project lib)
subsystems          1 fp missing  custom                (project lib)
labamp              0 fp missing  100% std-lib coverage <- Bug 2 board
Controller-Drone    0 fp missing  100% std-lib coverage <- Bug 2 board
bms_hw_voltmod      0 fp missing  100% std-lib coverage <- Bug 2 board
```

Even if we had EVERY missing lib, Bug 2 would cap the rescue at the long tail.

## Cumulative gain across the day on the 50-board set

| Layer | Δmean | Notes |
|---|---|---|
| Hybrid router (flash + pro escalation) | **+4.14 paired** | confirmed |
| Reconstruct shim fix (NerdNOS rescue) | **+1.9** | subsumed by Bug 2 fix |
| **Bug 2 fix (SetLayer not SetLayerAndFlip)** | **+36.57** | confirmed; 22/22 broken boards rescued; subsumes Bug 1 |
| **Total on flash@low** | **42.29 → 78.86** | huge transformation; broken cluster is gone |
| Hybrid router REapplied on top of Bug 2 | **TBD** | pro@low needs re-baseline; the broken-cluster rescue may eliminate most of pro's headroom |

## OpenRouter credit + cost notes

- Pro@low costs ~$0.12/board, flash@low ~$0.002/board (~60× ratio).
- Hybrid router routes 60% of boards to pro → 50-board batch ≈ $3.17.
- The 4-cell decomp×modelscan sweep ($/4-cell × 50 boards) burned the weekly
  cap on 2026-06-12. User raised it before pro@low full sweep.
- json_repair fix means parse failures no longer cost a re-run (silent salvage).

## File index — where to pick up

- `src/cua_smoketest/agent/planner_router.py` — KiCad escalation rule lives here.
- `src/cua_smoketest/agent/frontier_planner.py` — planner template
  (`_PLANNER_SYSTEM_KICAD`), `_metadata_text`, `_extract_json`, `FrontierPlanner`.
- `src/cua_smoketest/agent/kicad_runner.py` — runtime; new
  `planner_escalate_model` param.
- `scripts/kicad_reconstruct.py` — the SWIG shim that name-fallback was killing.
- `scripts/kicad_plan_ablation.py` — single-planner sweep across boards.
- `scripts/kicad_decomp_modelscan.py` — 4-cell driver (decomp on/off ×
  flash@med/pro@low).
- `scripts/kicad_pro_baseline.py` — full pro@low sweep + paired comparison
  vs flash@low.
- `scripts/kicad_lib_rescue_ablation.py` — broken-cluster rescue runner.
- `scripts/kicad_decompose_kicad_asset.py` — sub-circuit clustering (manifest
  builder).
- `scripts/build_jobs_default.py` — `KICAD_PLANNER_ESCALATE` constant +
  auto-injection of `--planner-escalate-model`.

## Diagnostic incantations

Profile broken cluster + lib demand:
```
python3 -c "import json,os,glob; from pathlib import Path; from collections import Counter; ..."
# (see Bug 1 section)
```

Re-run rescue with shim fix alone:
```
uv run python scripts/kicad_lib_rescue_ablation.py \
  --no-project-libs --planners google/gemini-3-flash-preview \
  --boards-list /tmp/kc_broken_boards.json \
  --out ~/cua_kicad_smoketest/lib_rescue_shimonly.jsonl
```

Re-run pro@low full sweep:
```
uv run python scripts/kicad_pro_baseline.py
```

Re-run hybrid simulation on the manifest:
```
python3 -c "
from cua_smoketest.agent.planner_router import select_planner_model
# pass each goal_png through and count escalations
"
```
