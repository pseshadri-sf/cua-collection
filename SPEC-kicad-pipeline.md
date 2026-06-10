# Engineering Spec — KiCad (PCB layout) trajectory-collection track

Status: **spec / design — no code written yet.** Date: 2026-06-10.
Source plan: [`TODO-kicad-pipeline.md`](TODO-kicad-pipeline.md).
Scope of this doc: turn the plan's conclusions into a concrete, file-level
implementation spec against the current `cua_smoketest` codebase, so the work
can be picked up phase-by-phase without re-deriving architecture.

This spec does **not** introduce a new pattern. It adds KiCad as a **third app**
behind the exact seams FreeCAD and Blender already use: a per-app *automation*
driver, an *action executor* + action-space spec, an *agentic runner*, a thin
*CLI wrapper script*, an app branch in the *frontier planner*, an app branch in
the *orchestrator*, and app branches in the *evaluator* + *postprocess* + *goal
pipeline*.

---

## 0. Mirror target: **FreeCAD**, not Blender

The codebase has two reference implementations of the
plan→code-block→live-canvas loop: the FreeCAD trio (`automation.py`,
`agent/action_space.py`, `agent/runner.py`) and the Blender trio
(`blender_*`). **KiCad mirrors FreeCAD.** The deciding factor is the
**console interaction model**, plus units, idempotency, and FreeCAD being the
higher-performing track.

KiCad's Scripting Console (`Tools → Scripting Console`) is a **dockable Qt-style
panel opened by a *toggling* menu item** — structurally identical to FreeCAD's
`View → Panels → Python console`, and nothing like Blender's workspace-switch
console. Every robustness fix already on the FreeCAD path applies directly;
a Blender-mirror would have to reinvent each one, worse:

| Concern | FreeCAD path (KiCad inherits) | Blender path (does NOT apply) |
|---|---|---|
| Console open | toggling menu item + `_console_opened` once-only latch | workspace cycle (console always present) |
| Open reliability | `open_python_console(verify=True)`: write a marker file via the console, confirm it appeared, retry the menu-open (commented *"the #1 cause of empty builds"*) | n/a |
| Long-line typing | write build code to a per-worker file, type a SHORT `exec(open(r'...').read())` line | `typewrite(code)` whole line char-by-char (fragile for long footprint/track lines) |
| Re-toggle bug | menu item TOGGLES → open once, never re-toggle | n/a (workspace) |
| Framing | in-console frame script via the app API (`Gui.SendMsgToActiveView("ViewFit")`) | real-input `frame_all` keystrokes |
| Units | millimetres (`Part.makeBox(x,y,z)` in mm) | unitless scene scale |
| Idempotency | reuse live doc handle: `doc=App.ActiveDocument or App.newDocument()` | clear whole scene (`select_all;delete`) |
| In-console state readback | `state_probe_path` exec'd after each build | n/a |

KiCad equivalents are 1:1: `b=pcbnew.GetBoard()` ≈ `App.ActiveDocument`;
`pcbnew.FromMM(x)` keeps the mm model; `pcbnew.Refresh()` + zoom-to-fit via the
API ≈ the FreeCAD `_FRAME_SCRIPT`.

**Genuinely KiCad-specific deltas (no FC or BL analogue)** — called out per
section below:
1. Cairo (Fallback) canvas forcing + software-GL env (`LIBGL_ALWAYS_SOFTWARE`,
   `GALLIUM_DRIVER=llvmpipe`) — KiCad's OpenGL canvas NULL-derefs under Xvfb.
2. **wxWidgets, not Qt** — KiCad is a wx app, so FreeCAD's `QT_QPA_PLATFORM=xcb`
   and the "Qt ignores synthetic SendEvent keys" quirk do **not** carry over.
   Synthetic-key/console-typing behaviour under wx is an M0 verification item.
3. A blank **seed board** the agent builds into (pcbnew needs an open `.kicad_pcb`).
4. The PCB action vocabulary (footprints/tracks/vias/zones/nets/outline).
5. A new **PCB reconstruction metric** (no Chamfer/volume analogue).

The shared, app-agnostic infrastructure (`DisplayManager`, `ScreenRecorder`,
`ScreenshotCapture`, `TrajectoryStep`/`TrajectoryResult`, `_write_json`,
loop-kill, postprocess, orchestrator lifecycle) is reused unchanged for KiCad,
exactly as it is for both existing apps.

---

## 0.1 Reference: how a FreeCAD job flows today

```
parallel_orchestrator.py
  ├─ discover/generate jobs  ──────────────► jobs JSONL  (app, goal_path, asset_path, extra_args, max_steps)
  ├─ pre-extract goal sidecars (--grounded) ─► <goal>.meta.json   via extract_goal_metadata.py
  ├─ build_worker_cmd(job) → picks SCRIPT by app
  │     freecad → scripts/agent_trajectory.py
  │     blender → scripts/blender_agent_trajectory.py
  │        └─ Runner.run():
  │             FrontierPlanner.plan()  → build_plan.json (full_code + steps)
  │             {App}Automation.launch() under Xvfb (DisplayManager)
  │             ActionExecutor(state_probe_path=…); executor.open_python_console(verify=True)
  │             ScreenRecorder.start()  (ffmpeg x11grab)
  │             compositional replay  OR  VLM action loop
  │                 executor.execute(action)  per step  (python_eval ⇒ file + exec(open(...)))
  │             ScreenRecorder.stop(); write trajectory.json
  │             postprocess_trajectory() → video_clean.mp4 (+ .meta.json)
  └─ evaluate: evaluate_{app}_run(traj, asset, dir) → score.match_score
```

KiCad slots a parallel branch into every one of those seams, copying the
FreeCAD modules.

---

## 1. New / changed files at a glance

| Area | File | New? | Mirrors |
|---|---|---|---|
| Process driver | `src/cua_smoketest/kicad_automation.py` | NEW | **`automation.py` (FreeCAD)** |
| Action space | `src/cua_smoketest/agent/kicad_action_space.py` | NEW | **`agent/action_space.py` (FreeCAD)** |
| Runner | `src/cua_smoketest/agent/kicad_runner.py` | NEW | **`agent/runner.py` (FreeCAD)** |
| CLI wrapper | `scripts/kicad_agent_trajectory.py` | NEW | `scripts/agent_trajectory.py` (FreeCAD) |
| Planner prompts | `src/cua_smoketest/agent/frontier_planner.py` | EDIT | add `app=="kicad"` arm (FC-style mm/idempotency) |
| Goal metadata | `scripts/extract_goal_metadata.py` | EDIT | add KiCad extractor (kicad-cli/pcbnew headless) |
| Goal render | `scripts/render_kicad_goal.py` | NEW | `render_goal_multiview.py` (kicad-cli) |
| Headless replay | `scripts/kicad_reconstruct.py`, `scripts/kicad_measure.py` | NEW | `freecad_reconstruct.py`/`freecad_measure.py` |
| Eval metric | `src/cua_smoketest/agent/kicad_eval.py` | NEW | (no analogue — see §6) |
| Eval dispatch | `src/cua_smoketest/agent/evaluator.py` | EDIT | add `evaluate_kicad_run` + `resolve_kicad_goal_asset` |
| Postprocess | `src/cua_smoketest/agent/postprocess.py` | EDIT | add `kicad` to `_CROP`/`_WINDOW` + `pcbnew_eval` to `_code_steps` |
| Orchestrator | `scripts/parallel_orchestrator.py` | EDIT | add `KICAD_SCRIPT`, `--app kicad`, dispatch |
| Jobs config | `scripts/build_jobs_default.py` | EDIT | add KiCad planner default |
| Procurement | `scripts/procure_kicad_assets.py` | NEW | `generate_*_assets.py` (kicad-happy clone+scan) |

Estimated new code surface: **~5 new modules + 2 headless scripts (~1,500 LOC)**
+ ~6 small edits. The runner/executor/automation trio is ~70% structural copy
of the **FreeCAD** trio with KiCad action bodies and the Cairo/wx deltas.

---

## 2. Infrastructure driver — `kicad_automation.py` (mirror `FreeCADAutomation`)

**Class `KiCadAutomation`** — same lifecycle contract as `FreeCADAutomation`
(`launch`/`quit`/`fit_view`/`_wait_for_window`/`_activate_window`/`_key`/
`_window_alive`). The wmctrl/xdotool/pid window-discovery machinery is copied
verbatim (it is toolkit-agnostic; identical in both FC and BL today).

```python
@dataclass
class KiCadLaunch:
    pid: int; window_id: str | None; window_title: str | None

class KiCadAutomation:
    def __init__(self, display, logs_dir,
                 pcbnew_binary=None,        # shutil.which("pcbnew")
                 window_timeout=90.0): ...
    def launch(self, board: Path) -> KiCadLaunch: ...   # always launches WITH a board
    def quit(self, timeout=5.0): ...
    def dismiss_dialogs(self): ...          # FC has no splash dismiss; KiCad may show path/rescue modals
```

### Deltas vs `FreeCADAutomation`

1. **Binary / launch arg**: `pcbnew <board.kicad_pcb>` (separate executable).
   Always launched *with* a board — `pcbnew.GetBoard()` needs an open document
   (FreeCAD also launches with an asset arg, so this matches; the difference is
   KiCad opens the **blank seed board**, §2.1, never the goal).

2. **Env — Cairo + software GL (KiCad-specific, replaces FC's Qt env):**
   ```python
   env["DISPLAY"] = self.display
   env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
   env.setdefault("GALLIUM_DRIVER", "llvmpipe")
   env.setdefault("LANG", "C.UTF-8")
   # NOTE: no QT_QPA_PLATFORM — KiCad is wxWidgets, not Qt.
   ```
   Requires `libgl1-mesa-dri` (provisioning, M0).

3. **Force the Fallback (Cairo) canvas** — OpenGL canvas NULL-derefs under
   Xvfb/llvmpipe (GitLab #11751). KiCad reads canvas choice from config, not a
   CLI flag. Mechanism decided in M0:
   - (a) seed `~/.config/kicad/10.0/pcbnew.json` (`canvas_type`/`gal_fallback`) +
     `kicad_common.json` to also suppress first-run dialogs; **or**
   - (b) toggle `View → Switch Canvas → Fallback` (`F11`/`F12` family) via the
     executor right after launch.
   Record the chosen keys here once known (parallels the `freecad_cli_quirks` memory).

4. **`fit_view`**: FreeCAD sends `0`,`v`,`f`. KiCad sends `Home` (Zoom to Fit)
   into the canvas. But primary framing happens **in-console** via the pcbnew
   API (see §3), mirroring FC's `_FRAME_SCRIPT`; `fit_view` is the belt-and-
   suspenders real-input fallback.

5. **Window-title match**: `["PCB Editor", "pcbnew", "KiCad"]`.

6. **`dismiss_dialogs()`**: send `Escape`×2 + `Return` for default buttons on the
   path-config / rescue modal pcbnew may show on first board open. Best-effort,
   idempotent. Exact dialogs enumerated in M0.

### 2.1 Per-job working board (KiCad-specific)

pcbnew needs an open `.kicad_pcb`, but the agent must build from scratch (no
File>Open of the goal). Contract: the runner copies a **blank seed board**
(`assets/kicad/_blank.kicad_pcb` — empty board, layers defined, no items) into
the job dir as `working.kicad_pcb` and launches `pcbnew working.kicad_pcb`. The
goal board is used only headless (goal render §7.3 + eval §6). This is the
KiCad analogue of FreeCAD launching into an empty doc.

---

## 3. Action space — `kicad_action_space.py` (mirror `ActionExecutor`)

**Primary action: `pcbnew_eval`** — the KiCad analogue of FreeCAD's
`python_eval`, built on the **same file + `exec(open(...).read())` mechanism**
(NOT Blender's direct char-by-char typing), because the long lines KiCad needs
(footprint loads, multi-segment tracks) type unreliably into a console.

### Module surface (copy `action_space.py`, swap bodies)

```python
VALID_TYPES = {
    # generic (identical bodies to FreeCAD ActionExecutor)
    "move_to","click","double_click","right_click","type","key","hotkey",
    "scroll","sleep","terminate",
    # kicad compound macros (mirror menu_navigate / python_eval / frame_view)
    "menu_navigate",            # opens Tools→Scripting Console (toggling item)
    "pcbnew_eval",              # PREFERRED: file+exec(open(...)) + in-console refresh/fit
    "frame_view",               # zoom-to-fit the canvas (Home), focus-independent
    # v1 structured PCB actions (translate to pcbnew_eval; planner-friendly)
    "place_footprint","move_footprint","route_track","add_via",
    "add_zone","set_board_outline","add_net","assign_pad_net",
}

PCBNEW_CONSOLE_INPUT_XY = (x, y)   # measured in M0 (mirror PYTHON_CONSOLE_INPUT_XY)
CANVAS_FOCUS_XY        = (x, y)    # mirror VIEWPORT_FOCUS_XY
MENU_PATHS = { ("Tools","Scripting Console"): [ (click/hover seq) ] }  # mirror FC MENU_PATHS

class KiCadActionExecutor:
    def __init__(self, state_probe_path=None): ...   # mirror ActionExecutor.__init__
    def open_scripting_console(self, verify=False): ...
    def execute(self, action) -> ExecutionResult: ...
```

### Inherited-from-FreeCAD machinery (the reason we mirror FC)

- **`_console_opened` once-only latch** — the `Tools → Scripting Console` menu
  item toggles, exactly like FC's `View → Panels → Python console`. Open once,
  never re-toggle (FC comment: re-toggling typed multi-step builds "into
  nothing" — 47% vs 80–94%).
- **`open_scripting_console(verify=True)`** — copy `open_python_console`: write a
  marker file via the console (`open(r'/tmp/cua_kicad_ok_PID','w').close()`),
  confirm it appeared, retry the menu-open up to 4× ("#1 cause of empty builds").
- **`_do_pcbnew_eval`** — copy `_do_python_eval`:
  1. write `code` to `/tmp/cua_kicad_eval_PID.py`;
  2. `open_scripting_console()` if not opened;
  3. focus `PCBNEW_CONSOLE_INPUT_XY`, type the SHORT line
     `exec(open(r'/tmp/cua_kicad_eval_PID.py').read());exec(open(r'/tmp/cua_kicad_frame.py').read())`,
     Enter, sleep ~1.5s (Cairo repaint);
  4. optional `state_probe_path` as a SEPARATE short submission;
  5. belt-and-suspenders real-input `frame_view` (focus canvas + `Home`).
- **`_FRAME_SCRIPT` analogue** (`/tmp/cua_kicad_frame.py`, written once in
  `__init__`): `import pcbnew; pcbnew.Refresh()` + zoom-to-fit via the API
  (`pcbnew.GetBoard()`'s frame, or the view-refresh call settled in M1). Focus-
  independent, like FC's `Gui.SendMsgToActiveView("ViewFit")`.
- Generic handlers (`_do_move_to/click/type/key/hotkey/scroll/sleep/terminate`,
  `_do_menu_navigate`, `_do_frame_view`, `_xy`) copied verbatim.

### KiCad deltas in the executor

- **wx, not Qt**: M0 verifies pyautogui `typewrite`/`press` land in the wx
  console the same way (FC's Qt-SendEvent-ignore quirk may or may not have a wx
  analogue). The file+exec approach already minimises typed-line length, which
  is the main mitigation regardless.
- **Console may float**: if M0 shows the console can't be docked to a fixed
  coord, replace the `PCBNEW_CONSOLE_INPUT_XY` constant with an
  `xdotool search --name "Python"`-relative click helper; everything else holds.

### Structured `pcbnew` macros (mirror `build_box`/`cut`/`fuse`)

Typed actions the executor renders into one `pcbnew_eval` line (validated for
positives/types like `_do_build_box`), then routed through `_do_pcbnew_eval`.
KiCad 10 SWIG bodies (mm via `pcbnew.FromMM`, `pcbnew.VECTOR2I`):

- `place_footprint{lib,name,ref,at:[x,y],rot_deg,layer}` → `FootprintLoad`,
  `SetReference`, `SetPosition(VECTOR2I(FromMM(x),FromMM(y)))`,
  `SetOrientationDegrees`, `board.Add(fp)`, `pcbnew.Refresh()`.
- `move_footprint{ref,at,rot_deg}`; `route_track{start,end,width_mm,layer,net}`
  → `PCB_TRACK`; `add_via{at,drill_mm,diameter_mm,net}` → `PCB_VIA`;
  `add_zone{outline,layer,net}` → `ZONE`+fill; `set_board_outline{rect|polygon}`
  → `PCB_SHAPE` on `Edge_Cuts`; `add_net{name}` → `NETINFO_ITEM`;
  `assign_pad_net{ref,pad,net}` → `pad.SetNet(...)`.

Layer strings → SWIG enums are resolved **inside the generated code line** (the
`F_Cu`/`B_Cu`/`Edge_Cuts` map is emitted as part of the exec'd file), so our
driver process never imports `pcbnew` (only the GUI interpreter has it).

### Action-space prose — `ACTION_SPACE_SPEC_KICAD`

Copy `ACTION_SPACE_SPEC`'s structure; swap the FreeCAD-specific entries
(`menu_navigate` path → `["Tools","Scripting Console"]`; `python_eval` →
`pcbnew_eval` with a pcbnew example; `frame_view` → "zoom-to-fit the PCB
canvas"). Keep the single-physical-line rule verbatim (the SWIG console is a
normal REPL).

---

## 4. Runner — `kicad_runner.py` (mirror `AgentTrajectoryRunner`)

**Class `KiCadAgentTrajectoryRunner`** — copy the **FreeCAD** `AgentTrajectoryRunner`
(not the Blender one). Reused verbatim: `TrajectoryStep`/`TrajectoryResult`,
`_write_json`, `_format_history`, `_seconds_to_mmss`, adaptive-reasoning
escalation, loop-kill + anti-loop directive, incremental `trajectory.json`
persistence, planner integration, postprocess hook, `_run_compositional` shape,
and crucially the FreeCAD console-init sequence:

```python
probe_path = self.output_dir / "_state_probe.py"; probe_path.write_text(_STATE_PROBE_KICAD)
executor = KiCadActionExecutor(state_probe_path=str(probe_path))
...
launch = kicad.launch(board=working_board)   # §2.1
executor.open_scripting_console(verify=True)  # marker-verified, AFTER GUI settles
```

### Deltas from the FreeCAD runner

| Concern | FreeCAD | KiCad |
|---|---|---|
| launch | `FreeCADAutomation` empty doc | `KiCadAutomation` on `working.kicad_pcb` (§2.1) |
| executor | `ActionExecutor` | `KiCadActionExecutor` |
| console init | `open_python_console(verify=True)` | `open_scripting_console(verify=True)` (same pattern) |
| action verb | `python_eval` | `pcbnew_eval` |
| state probe | FreeCAD object readback | pcbnew footprint/net/layer count readback |
| `_reset_state` | pgrep `-x freecad`/`FreeCAD` | pgrep `-x pcbnew` (same `CUA_WORKER_ID` parallel-mode guard) |
| system prompt | `SYSTEM_PROMPT` (Part API, STRATEGY B console) | `SYSTEM_PROMPT_KICAD` (pcbnew, Scripting Console strategy) |

### Auto-injected `frame_view` after each `pcbnew_eval`

FreeCAD's runner injects a synthetic `frame_view` step after every `python_eval`
(closed the viewport-staleness loop; Blender REVERTED this because it defeated
loop-kill there). **KiCad starts with the FreeCAD behaviour (inject), since it
shares FC's console+canvas model.** Whether Cairo needs it is an **M1
verification** — if `pcbnew.Refresh()` in the in-console frame script already
repaints reliably, drop the injection (matching the Blender lesson). Keep the
loop-kill `_auto` exclusion either way.

### System prompt — `SYSTEM_PROMPT_KICAD`

Copy `SYSTEM_PROMPT`'s skeleton: TASK (reconstruct the GOAL board layout from
scratch — no File>Open/append/import), `{action_space}` injection, UI landmarks
(PCB Editor canvas centre, console input row), "STRATEGY: Scripting console
(recommended)", CRITICAL one-line rules, HOUSEKEEPING (dismiss path/rescue modal
with Escape; Cairo is slow → `sleep` before screenshot). Worked example:

```
{"type":"menu_navigate","path":["Tools","Scripting Console"]}
{"type":"pcbnew_eval","code":"import pcbnew; b=pcbnew.GetBoard(); fp=pcbnew.FootprintLoad('Resistor_SMD.pretty','R_0805_2012Metric'); fp.SetReference('R1'); fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(20),pcbnew.FromMM(15))); b.Add(fp); pcbnew.Refresh()"}
{"type":"terminate"}
```

---

## 5. CLI wrapper — `scripts/kicad_agent_trajectory.py`

Copy `scripts/agent_trajectory.py` (FreeCAD) 1:1, changing:
- import `KiCadAgentTrajectoryRunner`;
- `OpenRouterVLMClient(..., app="kicad")` (new value, §5.1);
- keep `--planner-model/--planner-reasoning/--plan-format/--compositional/`
  `--grounded/--postprocess/--max-steps/--reasoning-effort/--image-max-dim` + the
  provider/env-file plumbing unchanged;
- accept `--best-of-both` as a **no-op** for orchestrator arg-symmetry (FreeCAD
  already does this, per commit `59eef59` — no Chamfer scorer for PCB in v1).

### 5.1 `vlm_client` app value

Add `"kicad"` to the recognized `app=` values. The KiCad action coercion (clamp
Qwen output to `pcbnew_eval`/structured macros + Strategy reminder) follows the
existing **Qwen-only branch** pattern (`qwen3_vl_action_schema` memory) — add a
`kicad` arm there, not a global change.

---

## 6. Frontier planner — edits to `frontier_planner.py`

Add a KiCad arm; `FrontierPlanner` already dispatches on `self.app`. The KiCad
prompts follow the **FreeCAD** conventions (mm units, reuse-and-clear-live-doc
idempotency), not Blender's.

### New prompt constants

- **`_PLANNER_SYSTEM_KICAD`** — "expert PCB-layout reconstruction PLANNER for the
  KiCad `pcbnew` SWIG API." Conventions (FC-style):
  - Units **mm via `pcbnew.FromMM`**; trust `GOAL_METADATA` (outline, footprint
    count, layer count, per-footprint ref/position/orientation).
  - **Outline → place footprints → nets → route → zones** ordering.
  - Place every footprint at its explicit `SetPosition` (PCB analogue of FC's
    "never leave parts at the origin" — overlapping footprints at (0,0) is the
    #1 failure mode this plan prevents).
  - `full_code`: one **idempotent** line reusing the open board —
    `import pcbnew; b=pcbnew.GetBoard(); [b.Remove(x) for x in list(b.GetFootprints())]; …; pcbnew.Refresh()`
    — directly mirroring the FC `doc=App.ActiveDocument or App.newDocument(); [doc.removeObject(...)]` contract.
  - OUTPUT schema identical to existing planners; `op` enum =
    `place_footprint|route_track|add_via|add_zone|set_board_outline|add_net`;
    `validation_targets:{footprint_count,net_count,layer_count,outline_bbox_mm}`.
- **`_COMPOSITIONAL_KICAD`** — appended in compositional mode (mirror
  `_COMPOSITIONAL_FC`'s structure: console namespace persists; each step ends
  `pcbnew.Refresh()`). Decomposition:
  - step 1: board outline (`PCB_SHAPE` on `Edge_Cuts`);
  - steps 2..k: one footprint each, placed;
  - then nets, then tracks (a net's tracks may group per step), then zones.
  - 3–15 steps; never zero; `full_code` = concatenation.
- **`_DECOMPOSE_KICAD`** — Stage-2 splitter (mirror `_DECOMPOSE_FC`).

### Wiring (small edits)

- `_plan_once`: `system = _PLANNER_SYSTEM_KICAD if app=="kicad" else …`.
- compositional append + `_decompose` system selection: add `kicad` arm.
- **`_validate_plan`**: currently requires `"doc" in fc or "bpy" in fc`. Extend
  to accept `"pcbnew" in fc or "GetBoard" in fc`.
- `render_plan_block`: app-agnostic; add a kicad `build_star` line format
  (`at`/`rot`/`layer`). `plan_best_of` stays Blender-only (no PCB scorer in v1).
- `_metadata_text`: add a `kicad` arm rendering `kicad.footprints` as the
  PER-PART table (ref/at/rot/layer) — the direct analogue of FC per-part
  bbox+origin grounding.

---

## 7. Goal pipeline

### 7.1 Procurement — `scripts/procure_kicad_assets.py`

Mirror `generate_*_assets.py`:
1. Clone the **`kicad-happy-testharness`** index; run its `checkout.py` to clone
   the repos (capped subset for v1).
2. Walk `*.kicad_pcb`; parse the S-expression for pad/net/layer/footprint counts
   + board-outline area.
3. **Difficulty filter** (plan §5): medium-to-difficult percentile band (same
   methodology as FC/Objaverse). Tunable thresholds; emit `manifest.jsonl`.
4. **License filter**: record per-repo license from `repos.md`; `--license-allow`
   to drop GPL if required. Default: keep all, record license.

Output layout (mirror `~/cua_gui_smoketest/`):
```
~/cua_kicad_smoketest/
  assets/<stem>.kicad_pcb                       # goal boards (never opened by the agent)
  screenshots/A_<n>_loaded_<stem>.png           # goal atlas (§7.3)
  screenshots/A_<n>_loaded_<stem>.meta.json     # sidecar (§7.2)
  manifest.jsonl
```

### 7.2 Goal metadata sidecar — `extract_goal_metadata.py` (KiCad arm)

The orchestrator pre-extracts `<goal>.meta.json` before running (§9). Add a
KiCad branch running under **`kicad-cli`/pcbnew headless** (no X). Superset
schema (keep existing keys; add `kicad{}`):

```json
{
  "asset": "/path/board.kicad_pcb", "app": "kicad",
  "bbox_mm": [W, D, 0],                 // board outline bbox (z=0)
  "object_count": <footprint_count>,    // reuse existing key
  "kicad": {
    "footprint_count": N, "net_count": N, "layer_count": N, "pad_count": N,
    "outline_bbox_mm": [W, D],
    "footprints": [{"ref":"R1","lib":"…","name":"…","at":[x,y],"rot":0,"layer":"F.Cu"}, …],
    "nets": ["GND","VCC", …]
  },
  "shape_descriptor": "<N footprints, M nets, K layers, WxD mm board>"
}
```

### 7.3 Goal render — `scripts/render_kicad_goal.py`

Mirror `render_goal_multiview.py`, via **kicad-cli (headless, no X)**:
- `kicad-cli pcb export svg|png` → flat 2D layer plot (top copper + silkscreen +
  edge cuts) — **primary** for layout match;
- `kicad-cli pcb render` → 3D board — secondary;
- compose a **multi-view atlas** (top-copper / silkscreen / 3D / drill-map),
  labels burnt in, one PNG (no downstream API change). Cache per stem under
  `/tmp/kicad_goal_cache/<stem>.png`; `--force` to rebuild.

The atlas `.meta.json` (§7.2) names the source `.kicad_pcb` under `asset` so the
evaluator + planner resolve it.

---

## 8. Eval metric — `kicad_eval.py` (the genuinely new piece)

No Chamfer/volume analogue for PCBs. Define a **composite PCB-reconstruction
score, 0–100**, computed headlessly by replaying the agent's `pcbnew_eval`
chunks onto the blank seed board and comparing to the goal `.kicad_pcb`.

### Reconstruction (mirror `evaluate_freecad_run`)

`evaluate_kicad_run(trajectory_json, goal_asset, work_dir)`:
1. `extract_python_chunks(traj, "kicad")` — extend `extract_python_chunks` to
   pull `pcbnew_eval` `code` (add `"pcbnew_eval"` alongside `"python_eval"`).
2. `scripts/kicad_reconstruct.py` (mirror `freecad_reconstruct.py`): headless
   pcbnew Python — `LoadBoard(seed)`, exec the chunks, `SaveBoard(out)` →
   `agent_model.kicad_pcb` + `agent_stats.json`.
   - **Headless `GetBoard()` caveat:** in a pure headless interpreter there is
     no GUI board. The replay shims `pcbnew.GetBoard` to return the
     `LoadBoard(seed)` handle (`pcbnew.GetBoard = lambda _b=_b: _b`) before
     exec'ing chunks. Validated in M3.
3. `scripts/kicad_measure.py` (mirror `freecad_measure.py`) measures the goal →
   `goal_stats`.

### Score components (`compare_kicad`)

| Component | Weight | Definition |
|---|---|---|
| footprint placement | 35 | Hungarian match by ref/value; per-match (Δpos,Δrot) within tolerance |
| connectivity / nets | 25 | net-set Jaccard + pad-net agreement; ERC-clean bonus |
| board outline IoU | 15 | IoU of agent vs goal `Edge_Cuts` polygon (2D) |
| layer-image similarity | 15 | SSIM/IoU of `kicad-cli pcb export png` plots (CPU; no GPU) |
| count agreement | 10 | footprint/net/layer count ratios (robust floor) |

Output dataclass `KiCadScore` with per-component fields + `match_score`,
`asdict`-serialized so the eval JSON keeps the **same top-level shape** as
`evaluate_freecad_run` (`app/goal_asset/agent_model/chunks_count/agent_stats/`
`goal_stats/score`) → `eval_parallel_run.py`/`rescore_parallel_run.py` work
unchanged. ERC/DRC via `kicad-cli` provide the validity oracle (the plan's
PCBSchemaGen "ERC+topology" half).

**Weights are a starting proposal** — tune on a handful of boards in M3/M4 via
`rescore_parallel_run.py`. Ship `kicad_measure.py` + `compare_kicad` first.

### Evaluator dispatch edits (`evaluator.py`)

- `KICAD_ASSETS_DIR = Path.home()/"cua_kicad_smoketest"/"assets"`.
- `resolve_kicad_goal_asset` — sidecar-first (`_asset_from_sidecar` already
  handles arbitrary names; regex arm maps `A_<n>_loaded_<stem>.png` →
  `<stem>.kicad_pcb`).
- `evaluate_kicad_run` (above). `GeometryStats`/`compare` stay for FC/BL; KiCad
  uses its own stats+compare in `kicad_eval.py`.

---

## 9. Orchestrator + config edits

### `parallel_orchestrator.py`

```python
KICAD_SCRIPT   = PROJECT_ROOT / "scripts" / "kicad_agent_trajectory.py"
KICAD_GOAL_DIR = Path.home() / "cua_kicad_smoketest" / "screenshots"
```
- `build_worker_cmd`: replace the binary `freecad?…:…` ternary with
  `{"freecad":FREECAD_SCRIPT,"blender":BLENDER_SCRIPT,"kicad":KICAD_SCRIPT}[job["app"]]`.
- `--app` choices → `("freecad","blender","kicad")`.
- `discover_goal_pngs`: add KiCad arm (`KICAD_GOAL_DIR`, same `A_*.png` glob).
- sidecar pre-extraction: extend app auto-detect (`"kicad" in str(gp)`); add the
  kicad resolver + extractor command (`kicad-cli`/headless pcbnew per M0).
- evaluation dispatch: add `evaluate_kicad_run` + `resolve_kicad_goal_asset`.
- `terminated_by` status mapping / retries / loop-kill: **unchanged** (KiCad runner
  emits the same vocabulary).

### `build_jobs_default.py`

Plan §4 (Flash ≥ Pro on PCBSchemaGen → cheap planner suffices):
```python
KICAD_PLANNER = "google/gemini-3.1-flash-preview"   # flash, not flash-lite (PCB spatial)
# app=="kicad": --model EXECUTOR --reasoning-effort low --image-max-dim 1024
#   --grounded --planner-model KICAD_PLANNER --compositional --planner-reasoning low --postprocess
```
Confirm flash vs flash-lite vs pro in the M5 A/B (mirror FC/BL) on ~25 boards
before locking the default.

### `postprocess.py`

Add `kicad` to `_CROP`/`_WINDOW` and `"pcbnew_eval"` to `_code_steps`'s type
check. Crop rect = the pcbnew **canvas rectangle** (excludes layers panel /
toolbar / status bar), measured in M1. KiCad keeps the canvas visible
throughout (console is a separate panel), so use a wide FreeCAD-style window
`(1.2, 0.8)` rather than Blender's tight `(0.5, 1.2)`.

---

## 10. Data contracts (unchanged shapes — new app value only)

- **`trajectory.json`**: identical schema; `action.type` ∈ kicad verbs.
  Downstream (`build_run_viz.py`, orchestrator status lift) needs no change.
- **`build_plan.json`**: identical; PCB `op` values are new strings only.
- **`<goal>.meta.json`**: superset (adds `kicad{}`); existing keys present.
- **eval JSON**: same top-level shape; `score` carries KiCad components +
  `match_score`.

Keeping these shapes means `eval_parallel_run.py`, `rescore_parallel_run.py`,
`build_run_viz.py`, `postprocess_run.py`, and the summary writers work for KiCad
with no edits beyond the §9 dispatch points.

---

## 11. Risks & mitigations

| Risk | Mitigation |
|---|---|
| OpenGL canvas crash under Xvfb | Force Cairo via config seed (§2.3); `LIBGL_ALWAYS_SOFTWARE`+`llvmpipe`+`libgl1-mesa-dri`. **M0 gate.** |
| wxWidgets ≠ Qt — synthetic-key/console-typing behaviour differs | M0 verifies pyautogui typing into the wx console; file+exec already minimises typed-line length (the main mitigation). |
| Menu-toggle console typed-into-nothing | Inherit FC's `_console_opened` once-latch + `open_scripting_console(verify=True)` marker check. |
| Floating console (no fixed coord) | Docked-seed for a fixed `PCBNEW_CONSOLE_INPUT_XY`, else `xdotool search`-relative click (§3). **M0 gate.** |
| `Refresh()` doesn't repaint on Cairo | In-console frame script + real-input `frame_view` fallback per step; settle inject-vs-not in M1 (FC injects; BL doesn't). |
| SWIG `pcbnew` deprecated (~KiCad 11) | **Pin KiCad 10.0.x**; isolate all SWIG behind `KiCadActionExecutor` + `kicad_reconstruct.py` so an IPC-API swap touches only those two. |
| `GetBoard()` undefined headless (eval) | `kicad_reconstruct.py` shims `GetBoard` to `LoadBoard(seed)` (§8). |
| No PCB metric exists | New composite (§8); ship measure+compare first, tune weights via `rescore_parallel_run.py`. **M3/M4.** |
| Placement/routing weak even for frontier | v1 targets placement+connectivity; routing/zones scored but not gated. |
| eeschema has no live API | **Out of scope v1** (PCB layout only); netlist generation, if needed, is headless/non-recorded upstream. |

---

## 12. Phased milestones (maps to plan §8) with acceptance criteria

- **M0 — Infra spike.** Provision KiCad 10 (PPA) + `libgl1-mesa-dri`. Launch
  `pcbnew working.kicad_pcb` under Xvfb+Cairo+software-GL; confirm canvas renders
  + ffmpeg `x11grab` captures it. Open the Scripting Console (verify the
  toggling-menu behaviour matches FC), run a `FootprintLoad`+`Add`+`Refresh`
  smoke; confirm a footprint **appears live**. **Verify wx synthetic-key typing.**
  Decide canvas-config + console-coord mechanisms; record the keys here.
  *Accept:* screenshot of one footprint placed via the console under Xvfb.

- **M1 — Driver + action space** (`kicad_automation.py`, `kicad_action_space.py`).
  Port the FC console machinery (`open_scripting_console(verify=True)`, file+exec
  `pcbnew_eval`, `_console_opened` latch, in-console frame script). Settle the
  `Refresh()`-vs-`frame_view`-injection question; measure the postprocess crop.
  *Accept:* `KiCadActionExecutor.execute({"type":"pcbnew_eval",…})` places a
  footprint marker-verified end-to-end; `frame_view` zooms to fit.

- **M2 — Runner + CLI + planner** (`kicad_runner.py`, `scripts/kicad_agent_trajectory.py`,
  `_PLANNER_SYSTEM_KICAD`/`_COMPOSITIONAL_KICAD`/`_DECOMPOSE_KICAD`, vlm `kicad` arm).
  End-to-end compositional run on one hand-made goal board.
  *Accept:* `trajectory.json` + `.mp4`; video shows outline→footprints→tracks
  step-by-step; `--postprocess` emits a clean video.

- **M3 — Goal pipeline + eval** (`procure_kicad_assets.py` subset,
  `extract_goal_metadata.py` kicad arm, `render_kicad_goal.py`,
  `kicad_reconstruct.py`/`kicad_measure.py`, `kicad_eval.py`).
  *Accept:* procured board → atlas + sidecar → agent run → `evaluate_kicad_run`
  returns a sane `match_score` on ~5 boards.

- **M4 — Metric validation.** Tune §8 weights on ~10 boards vs human "same board?"
  judgment; lock the composite.

- **M5 — Cost A/B.** flash-lite vs flash vs pro on ~25 boards (mirror FC/BL);
  confirm cheap-model ≥80% of frontier; set the `build_jobs_default.py` default.

- **M6 — Scale.** Run the medium-to-difficult band through
  `parallel_orchestrator.py --app kicad`; collect trajectories + clean videos.

- **Later.** Schematic-capture track (eeschema); SWIG→IPC-API migration for
  KiCad 11 (touches only `KiCadActionExecutor` + `kicad_reconstruct.py`);
  routing-quality scorer / best-of-N for placement hard cases.

---

## 13. Decisions still open (resolve during M0–M4)

1. **Canvas selection** — config-seed vs in-GUI `View→Switch Canvas` (M0).
2. **Console coords** — docked-fixed vs `xdotool`-relative (M0).
3. **wx synthetic-key typing** — does the file+exec line type reliably into the
   wx console; any wx analogue of FC's Qt-SendEvent quirk (M0).
4. **Per-step repaint** — in-console `Refresh()` sufficient, or keep the injected
   `frame_view` (M1).
5. **Eval weights** — §8 composite is a proposal; tune M3/M4.
6. **Planner tier** — flash vs flash-lite vs pro for PCB placement (M5).
7. **Asset subset size + license policy** for v1 procurement (M3).
```
