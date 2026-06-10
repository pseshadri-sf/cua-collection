"""Agentic trajectory runner for KiCad (pcbnew PCB layout).

Same shape as the FreeCAD AgentTrajectoryRunner (which it mirrors — see
SPEC-kicad-pipeline.md §0/§4): the console-init, file+exec eval path, state-probe
readback, auto-frame injection, and loop-kill all carry over. Deltas: launches
pcbnew on a blank SEED board (GetBoard() needs an open document), uses
KiCadActionExecutor + pcbnew_eval, and a pcbnew-specific system prompt + state
probe. (M0: the pcbnew Scripting Console is a floating "KiPython" PyShell window,
handled inside KiCadActionExecutor.)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..display import DisplayManager
from ..environment import EnvironmentInspector
from ..kicad_automation import KiCadAutomation
from ..screenshots import ScreenshotCapture
from .kicad_action_space import ACTION_SPACE_SPEC_KICAD, KiCadActionExecutor
from .screen_recorder import ScreenRecorder
from .vlm_client import OpenRouterVLMClient, _extract_goal_name
from .runner import TrajectoryStep, TrajectoryResult, _CompositionalDone


# Minimal blank 2-layer board pcbnew opens as the working document. The agent
# builds INTO this (it never opens the goal board — reconstruct-from-scratch).
# Used only if assets/kicad/_blank.kicad_pcb is absent. Format is M0-validated
# against KiCad 10 during the infra spike (SPEC §2.1).
_BLANK_BOARD = """\
(kicad_pcb (version 20221018) (generator pcbnew)
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
    (37 "F.SilkS" user)
    (38 "B.SilkS" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
)
"""


SYSTEM_PROMPT_KICAD = """You are an autonomous GUI agent controlling KiCad's PCB \
Editor (pcbnew, KiCad 10) on a Linux desktop (1920x1080) via pyautogui.

TASK: Visually recreate the GOAL_STATE PCB LAYOUT in pcbnew by CONSTRUCTING the
board from scratch — place footprints, route tracks, pour zones, and draw the
board outline. You are NOT allowed to load existing files — no File>Open,
File>Append Board, drag-and-drop, or import. Build everything from the open
(blank) board using the pcbnew Python API via the Scripting Console.

{action_space}

=== UI LANDMARKS (pcbnew 10, 1920x1080, default theme) ===

Menubar (y around 31): File, Edit, View, Place, Route, Inspect, Tools, ...
  The Scripting Console lives under Tools > Scripting Console; it opens as a
  floating "KiPython" window (the harness positions it for you).
PCB canvas occupies the centre; centre at roughly (960, 560). The "Home" key
  (Zoom to Fit) only acts on the canvas when the cursor is hovering over it.
Layers manager docks on the right.

=== STRATEGY: Scripting Console (recommended) ===
  Step 1: {{"type":"pcbnew_eval","code":"import pcbnew; b=pcbnew.GetBoard(); fp=pcbnew.FootprintLoad('/usr/share/kicad/footprints/Resistor_SMD.pretty','R_0805_2012Metric'); fp.SetReference('R1'); fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(20),pcbnew.FromMM(15))); b.Add(fp); pcbnew.Refresh()"}}
         -- Atomic: opens the console (once), runs the code via a file, and
         refreshes the canvas. b=pcbnew.GetBoard() is the LIVE board; every
         footprint/track/zone you Add() appears immediately. ALWAYS end code
         with pcbnew.Refresh().
  Step 2: {{"type":"frame_view"}}   -- Zoom to Fit if geometry is off-screen.
  Step 3: terminate when the layout visually matches GOAL_STATE.

CRITICAL RULES for pcbnew_eval `code`:
  - ALWAYS wrap millimetres with pcbnew.FromMM(...) and positions with
    pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)). Raw ints are nanometres.
  - Reference the layer by name via b.GetLayerID('F.Cu') / 'B.Cu' / 'Edge.Cuts'.
  - Place EVERY footprint at its own SetPosition — footprints left at (0,0)
    stack into one pile, the #1 failure mode this task exists to avoid.
  - Build order: board outline -> place footprints -> nets -> route tracks ->
    pour zones.

=== HOUSEKEEPING ===
  - If a path-config / rescue dialog appears on launch, press
    {{"type":"key","key":"escape"}} to dismiss it.
  - The Cairo software canvas is slow; after a build insert a
    {{"type":"sleep","seconds":1.5}} before relying on the screenshot.
  - If CURRENT_STATE already visually matches GOAL_STATE (footprints placed and
    routed in a similar arrangement), emit {{"action": {{"type": "terminate"}}, ...}}.

Output: exactly one JSON object per turn:
  {{"action": <action>, "rationale": "<one or two sentences>"}}
"""


# Executed inside the pcbnew Scripting Console after every build. Dumps the live
# board's footprint refs/positions + net/track counts so the runner can prove to
# the agent that its code executed and detect footprints stacked at the origin.
_STATE_PROBE_KICAD = '''\
import json as _J
try:
    import pcbnew as _P
    _b = _P.GetBoard()
    _fps = list(_b.GetFootprints()) if _b else []
    _objs = []
    for _f in _fps:
        _p = _f.GetPosition()
        _objs.append({"ref": _f.GetReference(),
                      "at": [round(_P.ToMM(_p.x), 2), round(_P.ToMM(_p.y), 2)]})
    _J.dump({"n": len(_objs), "footprints": _objs,
             "nets": int(_b.GetNetCount()) if _b else 0,
             "tracks": len(list(_b.GetTracks())) if _b else 0},
            open(r"__STATE_JSON__", "w"))
except Exception:
    pass
'''


def _split_pcbnew_full_code(full_code: str, target_steps: int = 10) -> list[dict] | None:
    """Deterministically split a P()-helper full_code build into visible steps so
    the board builds batch-by-batch on the canvas (the LLM compositional decompose
    is unreliable on high-footprint boards). The console namespace persists across
    pcbnew_eval submissions, so step 1 defines the helpers + draws the outline and
    later steps just call P(...). Returns None if the build isn't P()-based.

    Step 1   = setup (imports, clear, def P, def OUT, OUT(w,h)) -> empty outlined board.
    Step k>1 = a batch of P(...) footprint-placement calls; the last batch also
               carries any trailing statements (nets/tracks/Refresh)."""
    import math
    import re as _re
    lines = full_code.split("\n")
    p_idx = [i for i, ln in enumerate(lines) if _re.match(r"\s*P\(", ln)]
    if len(p_idx) < 2:
        return None  # not a P()-helper build — caller falls back
    first_p, last_p = p_idx[0], p_idx[-1]
    setup = lines[:first_p]
    pcalls = [lines[i] for i in p_idx]
    tail = [lines[i] for i in range(last_p + 1, len(lines)) if lines[i].strip()]
    batch = max(3, math.ceil(len(pcalls) / max(1, target_steps - 1)))
    steps = [{"code": "\n".join(setup), "name": "setup + outline",
              "why": "define helpers, clear board, draw outline"}]
    for k in range(0, len(pcalls), batch):
        chunk = pcalls[k:k + batch]
        last = (k + batch) >= len(pcalls)
        steps.append({
            "code": "\n".join(chunk + (tail if last else [])),
            "name": f"footprints {k + 1}-{k + len(chunk)}",
            "why": f"place {len(chunk)} footprints",
        })
    return steps


def _build_kicad_state_hint(state: dict) -> str:
    """Turn a pcbnew probe readout into an AGENT_STATE block (+ stacking repair
    recipe when footprints cluster at one point). Returns '' if not useful."""
    fps = state.get("footprints") or []
    n = state.get("n", len(fps))
    if n == 0:
        return (
            "AGENT_STATE: your last pcbnew_eval produced ZERO footprints on the "
            "board. The code ran but placed nothing — check that you called "
            "b.Add(fp) and pcbnew.Refresh(). Emit DIFFERENT code; do not re-run "
            "the same payload.\n\n"
        )
    lines = [f"  {f['ref']}: at={f['at']} mm" for f in fps[:10]]
    if len(fps) > 10:
        lines.append(f"  ... (+{len(fps) - 10} more)")
    hint = (
        f"AGENT_STATE (read back from the live board after your last build): "
        f"{n} footprint(s), {state.get('nets', 0)} net(s), "
        f"{state.get('tracks', 0)} track(s) —\n"
        + "\n".join(lines)
        + "\nYour code DID execute and these items are real. If the canvas looks "
        "empty or wrong, that is a FRAMING issue (use frame_view), NOT a reason "
        "to re-run identical code.\n"
    )
    # Stacking detector: >=2 footprints whose positions cluster within ~1mm.
    if len(fps) >= 2:
        xs = [f["at"][0] for f in fps]; ys = [f["at"][1] for f in fps]
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        if span < 1.0:
            hint += (
                f"\nMISSING_PLACEMENTS: your {len(fps)} footprints are all at "
                "~the same point — they are stacked at the origin. Each footprint "
                "needs its own SetPosition(pcbnew.VECTOR2I(...)) using the "
                "per-footprint position from the goal metadata. Re-emitting the "
                "same stacked code will NOT fix this.\n"
            )
    return hint + "\n"


class KiCadAgentTrajectoryRunner:
    def __init__(self, *, goal_png: Path, output_dir: Path,
                 vlm: OpenRouterVLMClient,
                 max_steps: int = 15,
                 display_manager: DisplayManager | None = None,
                 pcbnew_binary: str | None = None,
                 seed_board: Path | None = None,
                 kicad_post_launch_delay: float = 8.0,
                 post_action_delay: float = 0.6,
                 history_window: int = 10,
                 loop_kill_repeats: int = 5,
                 escalate_at_step: int = 0,
                 escalate_to_effort: str = "high",
                 planner_model: str | None = None,
                 plan_format: str = "python_eval",
                 planner_reasoning: str = "low",
                 compositional: bool = False,
                 postprocess: bool = False):
        self.compositional = compositional
        self.postprocess = postprocess
        self.planner_reasoning = planner_reasoning
        self.goal_png = Path(goal_png).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.vlm = vlm
        self.max_steps = max_steps
        self.display_manager = display_manager
        self.pcbnew_binary = pcbnew_binary
        self.seed_board = Path(seed_board).resolve() if seed_board else None
        self.kicad_post_launch_delay = kicad_post_launch_delay
        self.post_action_delay = post_action_delay
        self.history_window = history_window
        self.loop_kill_repeats = loop_kill_repeats
        self.escalate_at_step = escalate_at_step
        self.escalate_to_effort = escalate_to_effort
        self.planner_model = planner_model
        self.plan_format = plan_format

    def _prepare_working_board(self) -> Path:
        """Copy a blank seed board into the job dir as working.kicad_pcb. The
        agent builds into this; the goal board is never opened by pcbnew."""
        working = self.output_dir / "working.kicad_pcb"
        seed = self.seed_board
        if seed is None:
            # default repo seed: assets/kicad/_blank.kicad_pcb (2 dirs up = src/, repo root one more)
            repo_seed = Path(__file__).resolve().parents[3] / "assets" / "kicad" / "_blank.kicad_pcb"
            seed = repo_seed if repo_seed.exists() else None
        if seed and seed.exists():
            shutil.copyfile(seed, working)
        else:
            working.write_text(_BLANK_BOARD)
        return working

    def _run_compositional(self, plan, executor, capture, shots_dir, steps, t0,
                           json_path, video_path):
        """Build the board batch-by-batch via the off-screen console so the video
        is a clean viewfinder of the asset appearing step-by-step. Prefer a
        DETERMINISTIC split of full_code (helper + P() batches) over the unreliable
        LLM decompose, which collapsed high-footprint boards to one block. Returns
        terminated_by."""
        plan_steps = None
        fc = plan.get("full_code")
        if fc:
            plan_steps = _split_pcbnew_full_code(fc, target_steps=10)
        if not plan_steps:
            plan_steps = [s for s in plan.get("steps", []) if s.get("code")]
        if not plan_steps and fc:
            plan_steps = [{"code": fc, "name": "full",
                           "why": "full build (no per-step decomposition)"}]
        print(f"[compositional] KiCad replaying {len(plan_steps)} component steps "
              f"(off-screen console, deterministic split)", flush=True)
        for i, st in enumerate(plan_steps, start=1):
            self._write_json(json_path, steps, video_path=video_path,
                             terminated_by="in_progress", error=None,
                             model=self.vlm.model)
            action = {"type": "pcbnew_eval", "code": st["code"]}
            res = executor.execute(action)
            time.sleep(max(res.post_action_sleep, self.post_action_delay))
            # Zoom-fit the (console-free) canvas, then DWELL so the postprocess
            # window captures a clean, settled frame of the new board state.
            try:
                executor.execute({"type": "frame_view"})
            except Exception:
                pass
            time.sleep(1.2)
            png = shots_dir / f"step_{i:02d}_after.png"
            shot = capture.capture(png.name)
            if shot.path != png:
                shot.path.rename(png)
            # Record action_time at this settled, console-free moment.
            steps.append(TrajectoryStep(
                step_idx=i, action_time=time.monotonic() - t0, action=action,
                rationale=f"component {i}/{len(plan_steps)}: "
                          f"{st.get('name','')} — {st.get('why','')}",
                exec_error=res.error,
            ))
            time.sleep(1.0)  # video tail so the new state lingers on screen
        steps.append(TrajectoryStep(
            step_idx=len(plan_steps) + 1, action_time=time.monotonic() - t0,
            action={"type": "terminate"},
            rationale="all components built (compositional)",
        ))
        return "agent"

    def run(self) -> TrajectoryResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if not self.goal_png.exists():
            raise FileNotFoundError(f"goal image not found: {self.goal_png}")

        env_inspector = EnvironmentInspector()
        env = env_inspector.inspect()
        pcbnew_bin = (
            self.pcbnew_binary
            or env.tools.get("pcbnew")
            or shutil.which("pcbnew")
        )
        if not pcbnew_bin:
            raise RuntimeError("pcbnew binary not found")

        logs_dir = self.output_dir / "logs"
        shots_dir = self.output_dir / "frames"
        logs_dir.mkdir(parents=True, exist_ok=True)
        shots_dir.mkdir(parents=True, exist_ok=True)

        display_mgr = self.display_manager or DisplayManager(logs_dir)
        session = display_mgr.acquire()
        owns_display = not session.reused

        video_path = self.output_dir / "trajectory.mp4"
        json_path = self.output_dir / "trajectory.json"

        kicad = KiCadAutomation(
            display=session.display, logs_dir=logs_dir, pcbnew_binary=pcbnew_bin,
        )
        capture = ScreenshotCapture(shots_dir)

        # State probe: short /tmp paths keep the typed exec(open(...)) line short
        # so it lands reliably; unique per worker to avoid cross-worker clobber.
        wid = os.environ.get("CUA_WORKER_ID") or f"pid{os.getpid()}"
        state_json_path = Path(f"/tmp/cua_kicad_s_{wid}.json")
        probe_path = Path(f"/tmp/cua_kicad_s_{wid}.py")
        state_json_path.unlink(missing_ok=True)
        probe_path.write_text(_STATE_PROBE_KICAD.replace("__STATE_JSON__", str(state_json_path)))
        executor = KiCadActionExecutor(state_probe_path=str(probe_path),
                                       display=session.display)

        recorder = ScreenRecorder(
            display=session.display, output=video_path, logs_dir=logs_dir,
        )

        steps: list[TrajectoryStep] = []
        terminated_by = "max_steps"
        error: str | None = None
        agent_state_hint = ""
        system_prompt = SYSTEM_PROMPT_KICAD.format(action_space=ACTION_SPACE_SPEC_KICAD)

        # frontier-onepass planner (Variant A): one frontier call up front → a
        # pcbnew BUILD_PLAN injected as guidance every turn. Best-effort.
        plan_block = ""
        plan_obj = None
        if self.planner_model:
            try:
                from .frontier_planner import FrontierPlanner, render_plan_block
                # compositional=False even in compositional mode: KiCad splits the
                # plan's full_code DETERMINISTICALLY (_split_pcbnew_full_code), so
                # the LLM Stage-2 decompose is wasted (it also failed on real
                # boards). Skipping it saves a planner call per board.
                planner = FrontierPlanner(api_key=self.vlm.api_key,
                                          model=self.planner_model, app="kicad",
                                          image_max_dim=self.vlm.image_max_dim,
                                          reasoning_effort=self.planner_reasoning,
                                          compositional=False)
                plan = planner.plan(self.goal_png,
                                    goal_name=_extract_goal_name(self.goal_png))
                if plan:
                    plan_obj = plan
                    (self.output_dir / "build_plan.json").write_text(json.dumps(plan, indent=2))
                    plan_block = render_plan_block(plan, self.plan_format)
                    print(f"[planner] KiCad plan ready: {len(plan.get('steps', []))} steps "
                          f"compositional={self.compositional}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[planner] KiCad skipped ({type(exc).__name__}: {exc})", flush=True)

        try:
            self._reset_kicad_state()
            working_board = self._prepare_working_board()
            launch = kicad.launch(board=working_board)
            time.sleep(self.kicad_post_launch_delay)
            kicad.dismiss_dialogs()
            time.sleep(0.5)

            # Open the Scripting Console once, after the GUI settled, verify+retry.
            executor.open_scripting_console(verify=True)
            time.sleep(0.5)

            handle = recorder.start()
            t0 = handle.start_monotonic

            steps.append(TrajectoryStep(
                step_idx=0, action_time=0.0, action=None, rationale=None,
            ))

            if self.compositional and plan_obj and plan_obj.get("steps"):
                terminated_by = self._run_compositional(
                    plan_obj, executor, capture, shots_dir, steps, t0, json_path,
                    video_path)
                raise _CompositionalDone()

            for step_idx in range(1, self.max_steps + 1):
                if (self.escalate_at_step
                        and step_idx > self.escalate_at_step
                        and self.vlm.reasoning_effort != self.escalate_to_effort):
                    prev = self.vlm.reasoning_effort
                    self.vlm.reasoning_effort = self.escalate_to_effort
                    print(f"[escalate] step {step_idx}: reasoning_effort "
                          f"{prev!r} -> {self.escalate_to_effort!r}", flush=True)
                self._write_json(json_path, steps, video_path=video_path,
                                 terminated_by="in_progress", error=None,
                                 model=self.vlm.model)
                current_png = shots_dir / f"step_{step_idx:02d}_before.png"
                shot = capture.capture(current_png.name)
                if shot.path != current_png:
                    shot.path.rename(current_png)

                hist_hint = self._format_history(steps[-self.history_window:])
                recent_eval_codes = [
                    (s.action or {}).get("code")
                    for s in steps[-6:]
                    if (s.action or {}).get("type") == "pcbnew_eval"
                    and not (s.action or {}).get("_auto")
                ]
                if len(recent_eval_codes) >= 2 and recent_eval_codes[-1] == recent_eval_codes[-2]:
                    hist_hint = (
                        "ANTI-LOOP DIRECTIVE: Your last 2+ pcbnew_eval payloads "
                        "are IDENTICAL. The executor already refreshed + framed "
                        "the canvas after each. Re-emitting the same code will "
                        "not change the board and will trigger loop-kill. Your "
                        "NEXT action MUST be one of:\n"
                        "  - {\"type\":\"terminate\"} if CURRENT_STATE matches GOAL_STATE\n"
                        "  - {\"type\":\"pcbnew_eval\",\"code\":...} with DIFFERENT code "
                        "(place a missing footprint, route a net, fix a position)\n"
                        "  - {\"type\":\"frame_view\"} only if the canvas still shows "
                        "stale content\n"
                        "Do NOT re-emit the same pcbnew_eval a third time.\n\n"
                        + hist_hint
                    )
                if agent_state_hint:
                    hist_hint = agent_state_hint + hist_hint
                try:
                    resp = self.vlm.next_action(
                        system_prompt=system_prompt,
                        goal_png=self.goal_png,
                        current_png=current_png,
                        step_idx=step_idx,
                        max_history_hint=hist_hint,
                        plan_block=plan_block,
                    )
                except ValueError as exc:
                    steps.append(TrajectoryStep(
                        step_idx=step_idx, action_time=time.monotonic() - t0,
                        action=None, rationale=None, parse_error=f"parse: {exc}",
                    ))
                    continue
                except Exception as exc:  # noqa: BLE001
                    steps.append(TrajectoryStep(
                        step_idx=step_idx, action_time=time.monotonic() - t0,
                        action=None, rationale=None,
                        parse_error=f"vlm call failed: {exc}",
                    ))
                    error = str(exc)
                    terminated_by = "error"
                    break

                action = resp.action
                if isinstance(action, dict) and action.get("type") == "terminate":
                    after_t = time.monotonic() - t0
                    steps.append(TrajectoryStep(
                        step_idx=step_idx, action_time=after_t,
                        action=action, rationale=resp.rationale,
                        reasoning_trace=resp.reasoning_trace,
                        raw_content=resp.raw_content,
                        finish_reason=resp.finish_reason, usage=resp.usage,
                    ))
                    terminated_by = "agent"
                    break

                exec_result = executor.execute(action) if isinstance(action, dict) else None
                if exec_result is None:
                    steps.append(TrajectoryStep(
                        step_idx=step_idx, action_time=time.monotonic() - t0,
                        action=None, rationale=resp.rationale,
                        reasoning_trace=resp.reasoning_trace,
                        raw_content=resp.raw_content,
                        finish_reason=resp.finish_reason, usage=resp.usage,
                        parse_error="action was not a JSON object",
                    ))
                    continue

                time.sleep(max(exec_result.post_action_sleep, self.post_action_delay))
                after_t = time.monotonic() - t0
                steps.append(TrajectoryStep(
                    step_idx=step_idx, action_time=after_t, action=action,
                    rationale=resp.rationale,
                    reasoning_trace=resp.reasoning_trace,
                    raw_content=resp.raw_content,
                    finish_reason=resp.finish_reason, usage=resp.usage,
                    exec_error=exec_result.error,
                ))
                # Auto-inject a synthetic frame_view into history after a
                # successful pcbnew_eval (the executor already framed the canvas);
                # HISTORY-ONLY, breaks the identical-emit loop signal and surfaces
                # framing to the agent. Mirrors the FreeCAD runner.
                if (isinstance(action, dict)
                        and action.get("type") == "pcbnew_eval"
                        and exec_result.ok):
                    steps.append(TrajectoryStep(
                        step_idx=step_idx, action_time=after_t,
                        action={"type": "frame_view", "_auto": True},
                        rationale="(auto-injected: canvas was framed inside the pcbnew_eval executor)",
                    ))
                    agent_state_hint = ""
                    try:
                        state = json.loads(state_json_path.read_text())
                        agent_state_hint = _build_kicad_state_hint(state)
                        (self.output_dir / "agent_state.json").write_text(json.dumps(state))
                    except (OSError, ValueError):
                        pass
                if not exec_result.ok:
                    continue

                if self.loop_kill_repeats >= 2:
                    real = [s.action for s in steps
                            if s.action is not None and not s.action.get("_auto")]
                    tail = [json.dumps(a, sort_keys=True)
                            for a in real[-self.loop_kill_repeats:]]
                    if (len(tail) == self.loop_kill_repeats
                            and len(set(tail)) == 1):
                        print(f"[loop-kill] step {step_idx}: identical action "
                              f"emitted {self.loop_kill_repeats}x in a row — terminating",
                              flush=True)
                        terminated_by = "agent_loop_detected"
                        break

            time.sleep(1.0)
        except _CompositionalDone:
            pass
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            terminated_by = "error"
        finally:
            try:
                recorder.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                kicad.quit()
            except Exception:  # noqa: BLE001
                pass
            if owns_display:
                try:
                    display_mgr.release()
                except Exception:  # noqa: BLE001
                    pass

        success = terminated_by == "agent" and any(
            s.exec_error is None and s.action is not None for s in steps[1:]
        )
        self._write_json(json_path, steps, video_path=video_path,
                         terminated_by=terminated_by, error=error,
                         model=self.vlm.model)
        if self.postprocess:
            try:
                from .postprocess import postprocess_trajectory
                m = postprocess_trajectory(self.output_dir, "kicad")
                if m:
                    print(f"[postprocess] video_clean.mp4: {m['n_code_blocks']} blocks, "
                          f"{m['clean_duration_s']}s", flush=True)
                else:
                    print("[postprocess] skipped (no timed code steps / no video)", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[postprocess] failed: {type(exc).__name__}: {exc}", flush=True)
        return TrajectoryResult(
            video_path=video_path, json_path=json_path,
            goal_path=self.goal_png, steps=steps,
            success=success, terminated_by=terminated_by, error=error,
        )

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _reset_kicad_state() -> None:
        # Match only the pcbnew binary basename via pgrep -x; never kill our own
        # harness process. In parallel mode the orchestrator owns per-worker
        # lifecycle, so this process MUST NOT kill sibling workers' pcbnew.
        in_parallel_mode = bool(os.environ.get("CUA_WORKER_ID"))
        if shutil.which("pgrep") and shutil.which("kill") and not in_parallel_mode:
            res = subprocess.run(["pgrep", "-x", "pcbnew"],
                                 capture_output=True, text=True, timeout=5)
            for pid in res.stdout.split():
                try:
                    subprocess.run(["kill", "-9", pid], capture_output=True, timeout=5)
                except subprocess.SubprocessError:
                    pass
            time.sleep(1.5)

    @staticmethod
    def _format_history(recent: list[TrajectoryStep]) -> str:
        lines: list[str] = []
        for s in recent:
            a = json.dumps(s.action) if s.action else "null"
            lines.append(f"  step {s.step_idx} @ {s.action_time:6.2f}s: {a}")
        return "\n".join(lines) if lines else ""

    @staticmethod
    def _seconds_to_mmss(t: float) -> str:
        m = int(t) // 60
        s = t - 60 * m
        return f"{m:02d}:{s:05.2f}"

    def _write_json(self, json_path: Path, steps: list[TrajectoryStep],
                    video_path: Path, terminated_by: str,
                    error: str | None, model: str) -> None:
        payload: dict[str, Any] = {
            "goal_state": str(self.goal_png),
            "video": str(video_path),
            "model": model,
            "max_steps": self.max_steps,
            "terminated_by": terminated_by,
            "error": error,
            "trajectory": [],
        }
        for s in steps:
            payload["trajectory"].append({
                "step_idx": s.step_idx,
                "action_time": self._seconds_to_mmss(s.action_time),
                "action_time_seconds": round(s.action_time, 3),
                "action": s.action,
                "rationale": s.rationale,
                "reasoning_trace": s.reasoning_trace,
                "raw_content": s.raw_content,
                "finish_reason": s.finish_reason,
                "usage": s.usage,
                "parse_error": s.parse_error,
                "exec_error": s.exec_error,
            })
        json_path.write_text(json.dumps(payload, indent=2))
