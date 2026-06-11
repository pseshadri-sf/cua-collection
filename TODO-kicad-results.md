# KiCad track — procurement + tuning/ablation + full-collection results

Status: **M0–M6 DONE + full 100-board collection DONE.** KiCad 10.0.3 installed;
end-to-end pipeline validated and run at scale (100 real medium boards, 4
workers, clean step-by-step viewfinder videos). Last updated: 2026-06-11.

See [`SPEC-kicad-pipeline.md`](SPEC-kicad-pipeline.md) for the design and
[`TODO-kicad-pipeline.md`](TODO-kicad-pipeline.md) for the original plan.

## TL;DR (headline numbers)

| | result |
|---|---|
| boards collected | 100 real medium-difficulty (kicad-happy, 44–203 components) |
| full-run outcome | **100/100 succeeded, 0 failed, 100 clean videos** |
| performance | match_score mean **31.0** / median 27.6 / max 66.7 (bimodal — see M4) |
| speed | **66.5 min** wall, 4 workers, ~40 s/board, 1.50 boards/min |
| cost | **$1.79 total = $0.018/board** (gemini-3-flash planner only) |
| planner choice | **gemini-3-flash-preview** (flash ≈ pro, confirmed) |
| video | clean FreeCAD-style viewfinder, console off-screen, step-by-step |

The mean (31.0) is dragged down by ~40% of boards whose footprints aren't in the
system library (a data/env ceiling, NOT pipeline quality — those still produce
clean videos). On the reconstructable half, scores cluster at 50–67.

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
under Xvfb, with video. The outlier was `half-adder` (70→7 footprints): the
Stage-2 **LLM decompose** abbreviated the build. This exposed two video defects,
both since FIXED (next section). Artifacts: `~/cua_kicad_smoketest/runs/m6/<board>/`.

## Clean viewfinder videos (off-screen console + deterministic split) — FIXED

The first cleaned KiCad videos were a ~2 s static frame of the **console covering
the board**. Two root causes, both fixed:

1. **Console over the canvas.** KiCad's Scripting Console is a *floating* window
   ("KiPython"), unlike FreeCAD's docked panel, so the viewport crop captured it
   every frame. **Fix:** park the console PERMANENTLY OFF-SCREEN and type into it
   "blind" — `xdotool windowactivate` grants keyboard focus regardless of
   position, and XTEST keystrokes go to the focused window. The canvas is never
   obscured; *every* frame is a clean viewfinder, no hide/show flicker.
   (`kicad_action_space.py`: `CONSOLE_OFFSCREEN_XY`, `_submit_to_shell`.)
2. **Whole board in ONE code block** (`n_code_blocks: 1`). The LLM decompose
   failed on real boards, so `full_code` ran as a single `pcbnew_eval` → one
   captured moment. **Fix:** `_split_pcbnew_full_code` (`kicad_runner.py`)
   DETERMINISTICALLY splits the P()-helper `full_code` into a setup+outline step
   then batches of `P()` calls (~10 steps). The console namespace persists across
   `exec` submissions, so step 1 defines `P`/`b` and later steps call them.
   This REPLACES the unreliable LLM decompose (also skipped now → 1 fewer planner
   call/board). Also tightened the postprocess crop to canvas-only
   `(345,130,1295,890)`.

Result on esphome-dot (62 footprints): clean video **1 block/2.0 s → 10
blocks/20.0 s**, board builds component-by-component, console never in frame,
same 56/62 placement (namespace persistence confirmed). This is the video format
used by the full 100-board run below.

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

## Open items (next — prioritized)

1. **Footprint availability ceiling (biggest lever).** Only ~43% of footprints
   are loadable; this caps match_score (r=+0.97). Options: install more KiCad
   libraries (the `kicad-footprints` pkg is the standard set; project/custom
   footprints live in the source repos and aren't installable), OR filter
   procurement to boards whose footprints are mostly standard, OR extend the
   `_safe_fpl` fallback to also pull footprints embedded in the goal `.kicad_pcb`
   itself (the footprint definitions ARE in the goal board file).
2. **Connectivity / routing (breaks the ~67 ceiling).** `nets=0` everywhere —
   the planner places footprints + outline but doesn't reconstruct nets/tracks.
   Teach the planner to add nets + route (or down-weight the nets component if
   placement-only is the intended product). Routing is the deferred hard part.
3. **pcbnew loader crashes.** A minority of boards segfault `FootprintLoad` on a
   specific footprint (subprocess-isolated → scored 0). Could pre-screen boards
   by attempting a headless load during procurement and dropping crashers.
4. ~~Decompose fidelity on large boards~~ — **DONE** via the deterministic
   `_split_pcbnew_full_code` (replaced the lossy LLM decompose).

## Tooling added this pass (all committed, authored pseshadri-sf)

- `procure_kicad_assets.py --catalog` — catalog-driven medium-board sourcing.
- `kicad_plan_ablation.py` — headless planner A/B (parallelized planners).
- `kicad_metric_report.py` — ablation / metric analysis.
- `kicad_collect.py` — sequential GUI collector (per-board demo).
- `kicad_run_report.py` — performance/speed/cost report for an orchestrator run.
- planner `full_code` → multi-line `P()` helper (`frontier_planner.py`);
  `kicad_reconstruct.py` system-wide footprint fallback + `FootprintLoad` shim;
  off-screen console + `_split_pcbnew_full_code` for clean step-by-step videos.

---

## Environment, key paths & resume commands (for revisiting)

**Environment (installed this session):**
- KiCad **10.0.3** via `ppa:kicad/kicad-10.0-releases` (Ubuntu 22.04 jammy);
  binaries `pcbnew`, `kicad-cli`, `eeschema`; `pcbnew` importable from
  `/usr/bin/python3` (NOT the uv venv — eval/reconstruct scripts shell out to it).
- `kicad-footprints` + `kicad-symbols` (standard libs at
  `/usr/share/kicad/footprints/*.pretty`), `libgl1-mesa-dri`, `librsvg2-bin`.
- pcbnew runs under Xvfb with software GL (`LIBGL_ALWAYS_SOFTWARE=1
  GALLIUM_DRIVER=llvmpipe`); the first-run "KiCad Setup" wizard is auto-dismissed
  (Cancel→Yes via geometry-relative clicks — KiCad is wxWidgets, synthetic
  `--window` keys are dropped, only real clicks/XTEST register).

**Key data paths (under `~/cua_kicad_smoketest/`):**
- `assets/*.kicad_pcb` — the 100 staged goal boards.
- `manifest.jsonl` — per-board source repo + catalog complexity.
- `screenshots/<stem>.png` + `.meta.json` — goal atlases + grounding sidecars.
- `jobs_100.jsonl` — the orchestrator jobs file.
- `runs/full100/` — the full run: `summary.json`/`.csv`, `run_report.json`,
  `worker_*/jobs/<job_id>/` (each has `trajectory.json`, `video_clean.mp4`,
  `build_plan.json`, `eval/eval.json`).
- `ablation.jsonl` / `ablation_flash.jsonl` — M5 ablation data.
- Repo seed board: `assets/kicad/_blank.kicad_pcb` (the agent builds into a copy).
- kicad-happy index clone (if still present): `/tmp/kicad-happy-th/` (the catalog
  is `reference/repo_catalog.json`).

**Reproduce / resume (run from `/home/ubuntu/dev/init_envs_comp`, `uv run`):**

```bash
# 1) Procure N medium boards from the kicad-happy catalog (re-clone index if gone):
#    git clone --depth 1 https://github.com/aklofas/kicad-happy-testharness /tmp/kicad-happy-th
uv run python scripts/procure_kicad_assets.py --catalog /tmp/kicad-happy-th/reference/repo_catalog.json \
    --limit 100 --components-lo 44 --components-hi 203 --render

# 2) Render goal atlases (if not via --render). Parallel 4-way:
cat manifest_boards.txt | xargs -P4 -I{} uv run python scripts/render_kicad_goal.py --asset {} --out screenshots/{}.png

# 3) Headless planner ablation (cheap, no GUI):
uv run python scripts/kicad_plan_ablation.py --manifest ~/cua_kicad_smoketest/manifest.jsonl \
    --planners "google/gemini-3-flash-preview,google/gemini-3.1-flash-lite-preview,google/gemini-3.1-pro-preview" \
    --limit 25 --out ~/cua_kicad_smoketest/ablation.jsonl
uv run python scripts/kicad_metric_report.py --in ~/cua_kicad_smoketest/ablation.jsonl

# 4) Build jobs file + run the full GUI collection (4 workers, displays :200-:203):
#    (jobs builder is inline python in the session; extra_args from build_jobs_default.build_extra_args('kicad'))
uv run python scripts/parallel_orchestrator.py --app kicad --num-workers 4 --display-base 200 \
    --jobs-file ~/cua_kicad_smoketest/jobs_100.jsonl --output-dir ~/cua_kicad_smoketest/runs/full100

# 5) Report performance/speed/cost:
uv run python scripts/kicad_run_report.py --run-dir ~/cua_kicad_smoketest/runs/full100 \
    --start-file /tmp/kicad_full100_start.txt --workers 4

# Single-board GUI run (debug):
uv run python scripts/kicad_agent_trajectory.py --goal screenshots/<stem>.png \
    --output-dir /tmp/run --planner-model google/gemini-3-flash-preview \
    --compositional --grounded --postprocess
```

**Gotchas to remember:**
- `--display-base 200` to avoid collision with any other Xvfb pool (e.g. a
  concurrent FreeCAD/Blender run uses `:100`–`:109`).
- `FootprintLoad` needs the FULL `.pretty` path, and RAISES (not returns None)
  on a missing lib — both handled in the `P()` helper + reconstruct shim.
- Compositional mode never calls the VLM executor (qwen) — only the flash
  planner. The executor model in `extra_args` is inert there.
- The eval/reconstruct/measure scripts MUST run under `/usr/bin/python3` (has
  `pcbnew`), not the venv — handled internally by shelling out.
