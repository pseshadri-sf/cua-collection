# Adding KiCad (PCB design) to the trajectory-collection suite — Plan

Status: **research/planning only — no code written yet.** Date: 2026-06-10.
Goal: add KiCad as a third program (alongside FreeCAD + Blender) that an agent
reconstructs step-by-step in the GUI, recorded as a trajectory video, using the
same compositional planner→code-block→live-canvas pattern where possible.

> Headline conclusions
> 1. **Feasible and well-trodden** — KiCad runs headless under Xvfb (Cairo software canvas), and `pcbnew` has a live in-GUI Python console → the same "code block → live build → record" loop as FreeCAD/Blender.
> 2. **Scope the trajectory target to the PCB LAYOUT editor (`pcbnew`)**, not schematic capture. pcbnew has a live scriptable API; eeschema does **not**.
> 3. **Cheap models are enough** — on the PCBSchemaGen benchmark, Gemini-3-**Flash (88.1%) ≥ Gemini-3-Pro (86.1%)**. PCB code-gen is structured/parametric like FreeCAD → expect FreeCAD-like cost (~½¢/asset) with a flash/flash-lite planner. Frontier NOT required for ≥80% perf.
> 4. **Real editable assets exist at scale** — `kicad-happy-testharness` indexes **~5,800 open-source KiCad projects (~3,500 `.kicad_pcb`)** with a bulk-clone script.
> 5. **Biggest risks:** SWIG `pcbnew` bindings are deprecated (removal ~KiCad 11 → pin to 9/10 or migrate to the IPC API), and a new **placement/routing-aware eval metric** is needed (no Chamfer analogue).

---

## 1. Technical infrastructure (run + record like FreeCAD/Blender)

**Version:** KiCad **10.0.x** (2026). Editors are separate executables — launch directly with a file arg, exactly like our other apps:
`pcbnew board.kicad_pcb`, `eeschema sch.kicad_sch`, `kicad project.kicad_pro`.

**Headless under Xvfb, no GPU:** KiCad's GAL has a **Cairo software-rendering fallback canvas** (no GPU required), but the OpenGL canvas can crash under Xvfb/llvmpipe (`glXQueryDrawable`/`GLX_SWAP_INTERVAL_EXT` NULL-deref, GitLab #11751). **Mitigation:** force the **Fallback (Cairo) canvas** in pcbnew prefs + `LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe`. Install `libgl1-mesa-dri`.

**Install (Ubuntu server):** KiCad 10 PPA `ppa:kicad/kicad-10.0-releases` → native `pcbnew`/`eeschema`/`kicad-cli` (easiest under Xvfb). AppImage as fallback; avoid Flatpak (sandbox friction with Xvfb/x11grab).

**Video capture:** unchanged — `ffmpeg -f x11grab -framerate 10 -i :NN ... out.mp4` on the Xvfb display. The existing screen_recorder + `--postprocess` clean-video logic transfers directly (crop to the pcbnew canvas rect, cut code-entry dead-time, recalibrate timestamps).

**Goal images (headless, no X):** `kicad-cli` runs with no display.
- `kicad-cli pcb render board.kicad_pcb -o goal.png` → raytraced 3D board image (photorealistic, multi-side).
- `kicad-cli pcb export png/svg` → flat 2D layer plot (cleaner for layout matching).
- Also `pcb drc` / `sch erc` for validation. → This is our goal-render + (partial) eval oracle, analogous to `render_goal_multiview`.

**Prior art to lean on:** **KiAuto** (`INTI-CMNB/KiAuto`) already does Xvfb + xdotool GUI automation of pcbnew/eeschema *with built-in video recording* — direct reference implementation. Its docs warn xdotool GUI driving is "fragile" (focus-dependent) — same class of robustness work we did for FreeCAD's Qt SendEvent quirks and Blender's viewport framing.

**Recipe (mirrors current pipeline):** PPA install → Xvfb + Cairo canvas + software GL → launch `pcbnew board` → drive via the in-GUI Python console (primary) → record with ffmpeg → goal image via `kicad-cli`.

---

## 2. Action space — recommendation: **`pcbnew` live-console code blocks (PCB layout)**

The central question (GUI computer-use vs code-blocks/macros) resolves the same way as FreeCAD/Blender, **for the PCB editor**:

- **`pcbnew` SWIG Python module** fully builds a board: `FOOTPRINT` (place/move/rotate), `PCB_TRACK`/`PCB_VIA` (routing), `ZONE` (copper pours), `PCB_SHAPE` on `Edge_Cuts` (board outline), `NETINFO_ITEM` + `pad.SetNet()` (nets). `board = pcbnew.GetBoard()` returns the **live open board**; mutate it and call **`pcbnew.Refresh()`** to repaint → each footprint/track appears on canvas as it's added. **This is the FreeCAD/Blender compositional pattern exactly.** (Console: Tools → Scripting Console.)
- → **Use code blocks**, not pure computer-use, for PCB layout. Planner emits one-line `pcbnew` blocks; execute in the console with `Refresh()` after each → recordable step-by-step build (place components → route tracks → pour zones).

**Caveats / scoping decisions:**
- **eeschema (schematic) has NO live Python API.** Schematic *capture* would force pure GUI computer-use or non-animated headless file generation. → **Scope v1 to PCB layout (pcbnew) only.** Treat schematic capture as a later, harder track (or accept SKiDL/headless schematic generation as a non-recorded input that produces the netlist the layout is built against).
- **SWIG deprecation:** `pcbnew` SWIG bindings are deprecated as of KiCad 9 and slated for removal ~**KiCad 11**, and are unstable across major versions. → **Pin to KiCad 9/10** now; plan migration to the **IPC API** (`kicad-python`, Protobuf-over-socket, controls a live GUI out-of-process — same visible-build effect, different harness) for longevity.
- Undo/redo isn't integrated with script edits (irrelevant for our forward-only build).
- Cairo vs OpenGL refresh quirks: adding items + `Refresh()` paints reliably on Cairo; verify under our Xvfb setup (parallels the Blender `frame_all` viewport-repaint lesson).

**Alternative/secondary representations:** `.kicad_pcb` S-expression is human/LLM-writable but is *mutate-then-reload* (no animated GUI) — useful as ground-truth/diff, not for the video. SKiDL/atopile are connectivity/netlist DSLs (no layout animation).

---

## 3. text-to-KiCad baselines (agentic workflow, analogous to text-to-CAD / LL3M)

The field matured in late-2025/2026. Adopt one of these as the planner baseline:

- **circuit-synth** (`circuit-synth/circuit-synth`) — *closest analogue to our text-to-CAD setup*: Python-defined circuits emit native `.kicad_pro/.kicad_sch/.kicad_pcb/.kicad_net`, with a built-in Claude-Code agent suite + an MCP server (`mcp-kicad-sch-api`). Generates schematic/netlist; does **not** auto-place/route.
- **skidl-skills** (`nickkraakman/skidl-skills`) — clean 9-agent NL→SKiDL→netlist pipeline; good reference for agentic structure (~80% to a working PCB, placement/routing manual).
- **PCBSchemaGen** (arXiv 2602.00510, `HZou9/PCBSchemaGen`) — strongest **academic baseline + benchmark**: training-free LLM→**SKiDL** with multi-stage verification (syntax→ERC→topology) + feedback loop; 23-task benchmark. **Use its benchmark as our primary schematic-synthesis metric.**
- **atopile** (`atopile/atopile`, used by Diode) — typed `.ato` hardware language compiling to KiCad; "datasheet→ato" LLM path. Stronger validation than raw SKiDL.
- **KiCad MCP servers:** `mixelpixx/KiCAD-MCP-Server` (full read/write via pcbnew scripting — only one that *creates* designs); `lamaalrajih/kicad-mcp` (read-only/analysis via IPC API — good for reconstruction *verification*).

**Recommended baseline:** planner → **`pcbnew` layout code blocks** (for the recordable build) anchored on a goal board; use **circuit-synth/SKiDL** as the connectivity representation when a netlist is needed; evaluate with **PCBSchemaGen** (+ PCB-Bench for placement/routing reasoning).

---

## 4. Cost & performance — **cheap models suffice (frontier not required)**

PCBSchemaGen Pass@1 (its 23-task benchmark, with verification loop):

| model | Pass@1 |
|---|---|
| **Gemini-3-Flash** | **88.1%** |
| Gemini-3-Pro | 86.1% |
| GPT-5.1 | 83.2% |
| DeepSeek-V3.2 | 73.0% |
| GPT-OSS-120B | 60.0% |
| Llama-4-Maverick | 37.4% |

**Flash ≥ Pro here** — PCB schematic/netlist code-gen is structured & heavily represented in LLM training data, so a flash/flash-lite planner should hit ≥80% (indeed ~100%) of frontier performance. This mirrors our FreeCAD result (flash-lite ≈ pro at 1/20th cost). **Expect KiCad to match the FreeCAD cost/speed profile: ~½¢/asset with `gemini-3-flash`/`flash-lite` + low reasoning.** Frontier reserved only if needed.

**The hard part is placement & routing** (spatial), weak even for frontier (component overlap, imperfect routes) — analogous to Blender organics. Mitigations: a `--best-of-both`-style candidate-and-score loop, or accept netlist/placement-level reconstruction first and add routing later. Target: same ~90s/trajectory wall (GUI-replay bound, planner-agnostic) as FreeCAD.

**Plan:** default planner = `gemini-3-flash`/`flash-lite` + low; A/B vs pro on a small set (as we did for FC/BL) to confirm the ≥80% bar on *our* reconstruction task + metric.

---

## 5. Asset datasets (download targets)

**Primary: `kicad-happy-testharness`** (`aklofas/kicad-happy[-testharness]`) — curated, deduplicated index of **~5,822 open-source KiCad projects**: **6,845 `.kicad_sch`, 3,498 `.kicad_pcb`, 312,956 parsed components**. Real *editable* board files (not images), `repos.md` with URLs + pinned commits + categories, bulk-clone via `checkout.py`. Licenses are per-upstream (mixed MIT/GPL/CERN-OHL/CC) — filter post-clone. **This is the procurement source** (analogous to FreeCAD-library / Objaverse).

**Supplements:** KiCad official demos (17, clean multi-complexity); Olimex/SparkFun/Adafruit vendor repos; OSHWA certified list (3,200+, API-filterable by license); CERN's ~17k-component KiCad library + KiCad official footprints (CC-BY-SA) as building-block assets.

**NOT usable as reconstruction targets:** academic PCB datasets are **images/graphs, not editable boards** — CircuitNet (IC, not PCB), DeepPCB (defect images), FICS-PCB/FPIC (images), BenchPCNP (netlist graphs). Treat as out of scope.

**Difficulty filter** (parse from `.kicad_pcb`): pad count, net count, layer count, footprint count, board area → medium-to-difficult band, same methodology as the FreeCAD/Objaverse selection.

---

## 6. New considerations specific to KiCad (not present in FC/BL)

- **Goal representation:** a PCB goal is 2D layout + 3D render. Use `kicad-cli pcb export svg/png` (2D layer plot, best for layout matching) and/or `pcb render` (3D). Likely a **multi-view-style goal** (top copper / silkscreen / 3D), echoing our multi-view atlas.
- **Eval metric (NEW — no Chamfer analogue):** need a PCB-reconstruction metric. Candidates: footprint **placement match** (position/orientation of components vs goal), **netlist/connectivity overlap** (ERC-clean + net set match), **board-outline IoU**, and **rendered-image similarity** of the layer plots (CPU; SSIM/IoU since no GPU). Likely a composite. PCBSchemaGen's ERC+topology verification is the connectivity half. **This metric design is an open task before benchmarking.**
- **2-track structure:** (a) **layout reconstruction** in `pcbnew` (recordable, the trajectory product); (b) optional **schematic/netlist generation** upstream (SKiDL/circuit-synth, headless, not recorded) to supply nets the layout builds against.

---

## 7. Reuse from the existing pipeline (low new-code surface)

- `parallel_orchestrator.py` (add a `kicad` app route → `kicad_agent_trajectory.py`), `screen_recorder.py`, `--postprocess` (crop+cut+recalibrate), goal sidecar/grounding, build_run_viz viewer, jobs-file + extra_args plumbing, per-app planner config in `build_jobs_default.py`.
- New code needed: a `KiCadActionExecutor` (open scripting console, run `pcbnew` code blocks + `Refresh()`), a `kicad_runner.py` (mirror of blender_runner with compositional replay), a KiCad planner prompt (`_PLANNER_SYSTEM_KICAD` targeting `pcbnew` layout), a goal-render step (`kicad-cli`), a difficulty-scan/selection over kicad-happy, and the PCB eval metric.

---

## 8. Phased implementation plan

1. **Infra spike:** install KiCad 10 (PPA), launch `pcbnew` under Xvfb + Cairo + software GL, confirm canvas renders + ffmpeg captures; open the scripting console and run a `pcbnew.GetBoard()` + add-footprint + `Refresh()` smoke (verify live repaint, à la BL `frame_all`).
2. **Goal pipeline:** procure a small set from `kicad-happy-testharness`; difficulty-scan (`.kicad_pcb` pad/net/layer counts); render goals via `kicad-cli`.
3. **Action space + planner:** `KiCadActionExecutor` + `_PLANNER_SYSTEM_KICAD` (pcbnew layout, compositional: outline → place footprints → nets → route → zones); adopt circuit-synth/SKiDL or PCBSchemaGen as the baseline reference.
4. **Eval metric:** implement placement + connectivity + layer-image similarity composite; validate on a handful.
5. **Cost A/B:** flash-lite vs pro planner on ~25 boards (mirror FC/BL ablation) → confirm cheap-model ≥80%.
6. **Scale:** per-app-optimal config into `build_jobs_default.py`; collect trajectories + clean videos.
7. **Later:** schematic capture track; SWIG→IPC-API migration for KiCad 11.

---

## Sources
KiCad headless/CLI: docs.kicad.org/master/en/cli, dev-docs.kicad.org (pcbnew bindings, IPC API, file formats), GitLab kicad #11751, KiAuto (INTI-CMNB/KiAuto), KiCad PPA. •
Action space: pcbnew_python_scripting.adoc, dev-docs PCB bindings, SKiDL (devbisme/skidl), atopile, atait/kicad-python. •
Baselines/benchmarks: circuit-synth, nickkraakman/skidl-skills, PCBSchemaGen (arXiv 2602.00510), PCB-Bench (ICLR 2026), HWE-Bench (arXiv 2603.18102), LayoutCopilot (arXiv 2406.18873), mixelpixx/KiCAD-MCP-Server, lamaalrajih/kicad-mcp. •
Datasets: aklofas/kicad-happy[-testharness], KiCad demos, OSHWA cert list/API, Olimex/SparkFun/Adafruit, CERN KiCad lib, KiCad official footprints (GitLab). •
Cost: PCBSchemaGen Pass@1 table.
