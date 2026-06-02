"""End-to-end agentic trajectory runner.

Bootstraps:
  - kills any leftover FreeCAD process and clears stale recovery state
  - acquires an X display (via DisplayManager from cua_smoketest.display)
  - launches FreeCAD into a blank state (no asset)
  - starts an ffmpeg screen recording of the whole display
  - loops at most `max_steps` times:
      * captures the current display as PNG
      * asks the VLM for the next action given (system, goal, current)
      * executes the action via pyautogui
      * records the post-action timestamp as the step's `action_time`
  - terminates on `terminate` action or step exhaustion
  - writes a JSON trajectory log (with full per-step reasoning) and
    returns paths to video + JSON
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..automation import FreeCADAutomation
from ..display import DisplayManager, DisplaySession
from ..environment import EnvironmentInspector
from ..screenshots import ScreenshotCapture
from .action_space import ACTION_SPACE_SPEC, ActionExecutor
from .screen_recorder import ScreenRecorder
from .vlm_client import OpenRouterVLMClient, VLMResponse


SYSTEM_PROMPT_TMPL = """You are an autonomous GUI agent controlling FreeCAD 0.19 \
on a Linux desktop (1920x1080) via pyautogui.

TASK: Visually recreate the GOAL_STATE in FreeCAD by CONSTRUCTING the
geometry from scratch using FreeCAD's own modeling tools. You are NOT
allowed to load existing files — do not use File>Open, File>Recent,
drag-and-drop, or any other file-loading mechanism. The geometry in
the CURRENT_STATE must be built using FreeCAD's primitives, sketcher,
Part workbench operations, or the Python console.

{action_space}

=== EXACT UI COORDINATES (measured for FreeCAD 0.19, 1920x1080, default theme) ===

Menubar (y=10 for all):
  File   -> x=20      Edit   -> x=58      View   -> x=99
  Tools  -> x=144     Macro  -> x=194     Windows-> x=255     Help -> x=315
  (Part menu only appears AFTER switching to the Part workbench, then
   slots in between Macro and Windows around x=220.)

Top toolbar row 1 (y=40):
  New doc      -> (30, 40)
  Open file    -> (63, 40)
  Save         -> (97, 40)
  Cut/Copy/Paste -> (175/210/245, 40)
  WORKBENCH SELECTOR (wide dropdown showing "Start"): center is (550, 40).
    Click this, then click "Part" in the resulting dropdown list (the
    dropdown opens DOWNWARD from (550, 40); "Part" is usually 5-8 rows
    down, around (550, 200)). Wait ~1s after the click for the list.

3D viewport: roughly (290..1900, 105..1030). Centre at (1100, 540).
  V,F (Fit All) and "0" (Isometric) shortcuts ONLY work when the
  cursor is hovering inside this region. Move the cursor there with a
  move_to action BEFORE pressing those keys.

=== STRATEGY A: Part workbench primitive ===
  Step 1: {{"type":"switch_workbench","name":"Part"}}
  Step 2: {{"type":"menu_navigate","path":["Part","Primitives","Box"]}}
  Step 3: {{"type":"focus_viewport"}}     -- transfers keyboard focus to 3D view
  Step 4: {{"type":"key","key":"0"}}      -- isometric
  Step 5: {{"type":"key","key":"v"}} then {{"type":"key","key":"f"}}   -- fit all

=== STRATEGY B: Python console (most reliable; recommended) ===
  Step 1: {{"type":"menu_navigate","path":["View","Panels","Python console"]}}
         -- docks the Python console at the bottom of the window.
  Step 2: click the console's input field, near (700, 990), to focus it.
  Step 3: type a single-line Python snippet. For a box:
            doc=App.newDocument();import Part;b=Part.makeBox(50,70,30);o=doc.addObject('Part::Feature','Box');o.Shape=b;doc.recompute()
  Step 4: press Enter
  Step 5: {{"type":"focus_viewport"}}     -- CRUCIAL: console still has
         keyboard focus after step 4. Without focus_viewport, the next
         "0"/"v"/"f" keystrokes get typed into the Python console
         instead of acting on the 3D view, leaving the camera in
         TOP orthographic view and causing SyntaxErrors next turn.
  Step 6: {{"type":"key","key":"0"}}      -- isometric
  Step 7: {{"type":"key","key":"v"}} then {{"type":"key","key":"f"}}

IMPORTANT: Prefer the compound actions menu_navigate and switch_workbench
over manual chained clicks. Single bare clicks on menu items between
turns let the dropdown auto-close, breaking navigation. The compound
actions execute the full hover+click sequence in one shot with the
correct Qt-friendly timing.

=== HOUSEKEEPING ===
  - Ctrl+N opens a new empty document. The Start page is informational
    only and contains no geometry.
  - Software OpenGL renders slowly. After creating geometry or switching
    workbenches, insert a {{"type":"sleep","seconds":1.5}} action before
    issuing the next click so the GUI fully repaints.
  - If a "Document Recovery" dialog appears on launch, press "key":
    "escape" to dismiss it before doing anything else.
  - If CURRENT_STATE already visually matches GOAL_STATE (a recognisable
    3D shape in the viewport matching the goal's shape, with similar
    camera framing), emit {{"action": {{"type": "terminate"}}, ...}}.

Output: exactly one JSON object per turn:
  {{"action": <action>, "rationale": "<one or two sentences>"}}
"""


@dataclass
class TrajectoryStep:
    step_idx: int
    action_time: float          # seconds from video start (action's AFTER frame)
    action: dict | None
    rationale: str | None
    reasoning_trace: str | None = None     # full model chain-of-thought
    raw_content: str | None = None         # raw text the model emitted
    finish_reason: str | None = None
    usage: dict | None = None
    parse_error: str | None = None
    exec_error: str | None = None


@dataclass
class TrajectoryResult:
    video_path: Path
    json_path: Path
    goal_path: Path
    steps: list[TrajectoryStep]
    success: bool
    terminated_by: str  # "agent" | "max_steps" | "error"
    error: str | None = None


# Wave-9 S2: executed inside the FreeCAD Python console after every build.
# Dumps each solid's bbox center + dims so the runner can prove to the agent
# that its code DID execute (refuting the "viewport stale -> re-run" loop) and
# can detect parts stacked at the origin.
_STATE_PROBE_SRC = '''\
import json as _J, FreeCAD as _FC
_d = _FC.ActiveDocument
_objs = []
for _o in (_d.Objects if _d else []):
    _sh = getattr(_o, "Shape", None)
    try:
        if _sh is None or _sh.isNull():
            continue
        _bb = _sh.BoundBox
        _objs.append({
            "name": _o.Name,
            "center": [round(_bb.Center.x, 1), round(_bb.Center.y, 1), round(_bb.Center.z, 1)],
            "dims": [round(_bb.XLength, 1), round(_bb.YLength, 1), round(_bb.ZLength, 1)],
        })
    except Exception:
        pass
_J.dump({"n": len(_objs), "objects": _objs}, open(r"__STATE_JSON__", "w"))
'''


def _build_agent_state_hint(state: dict) -> str:
    """Turn a probe readout into an AGENT_STATE block (+ A2 repair recipe when
    the built parts are stacked at one point). Returns '' if nothing useful."""
    objs = state.get("objects") or []
    n = state.get("n", len(objs))
    if n == 0:
        return (
            "AGENT_STATE: your last python_eval produced ZERO objects in the "
            "document. The code ran but built nothing visible — check that you "
            "added the shape to the document and called doc.recompute(). Emit "
            "DIFFERENT code; do not re-run the same payload.\n\n"
        )
    lines = [
        f"  {o['name']}: center={o['center']} dims={o['dims']}"
        for o in objs[:8]
    ]
    if len(objs) > 8:
        lines.append(f"  ... (+{len(objs) - 8} more)")
    hint = (
        f"AGENT_STATE (ground truth read back from the FreeCAD document after "
        f"your last build): {n} object(s) exist —\n"
        + "\n".join(lines)
        + "\nYour code DID execute and these solids are real. If the viewport "
        "looks empty or wrong, that is a FRAMING issue, NOT a reason to re-run "
        "identical code. If these objects do not match the goal, change your "
        "code (dimensions, positions, or primitive type).\n"
    )
    # A2: stacking detector — >=2 objects whose centers cluster within half the
    # largest part dimension means everything was built at the origin with no
    # translate, which renders as a single blob.
    if len(objs) >= 2:
        cs = [o["center"] for o in objs]
        span = max(max(c[i] for c in cs) - min(c[i] for c in cs) for i in range(3))
        maxdim = max((max(o["dims"]) for o in objs), default=1.0) or 1.0
        if span < 0.5 * maxdim:
            hint += (
                "\nMISSING_TRANSLATIONS: your "
                f"{len(objs)} parts are all centered at ~the same point — they "
                "are stacked at the origin, so they look like one object. A "
                "multi-part goal needs each part MOVED to its own position. "
                "Apply a translate to every part BEFORE adding it, using the "
                "origin from PER-PART DECOMPOSITION, e.g.:\n"
                "  p1.translate(App.Vector(X, Y, Z))\n"
                "Re-emitting the same stacked code will NOT fix this.\n"
            )
    return hint + "\n"


class AgentTrajectoryRunner:
    def __init__(self, *, goal_png: Path, output_dir: Path,
                 vlm: OpenRouterVLMClient,
                 max_steps: int = 15,
                 display_manager: DisplayManager | None = None,
                 freecad_binary: str | None = None,
                 freecad_post_launch_delay: float = 4.0,
                 post_action_delay: float = 0.6,
                 history_window: int = 10,
                 # Wave-8: bumped from 3 -> 5 to give the agent extra retries
                 # after viewport auto-fit (item A in action_space.py) puts
                 # the built geometry on screen, which often takes 1-2 turns
                 # for the agent to recognize.
                 loop_kill_repeats: int = 5,
                 escalate_at_step: int = 0,
                 escalate_to_effort: str = "high"):
        self.goal_png = Path(goal_png).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.vlm = vlm
        self.max_steps = max_steps
        self.display_manager = display_manager
        self.freecad_binary = freecad_binary
        self.freecad_post_launch_delay = freecad_post_launch_delay
        self.post_action_delay = post_action_delay
        self.history_window = history_window
        # Loop-kill: if the agent emits `loop_kill_repeats` consecutive
        # identical canonical action payloads, force-terminate. Set to 0
        # to disable. Default 3 — diagnosed in the sweep120 analysis as
        # the dominant failure mode: 31/31 max_steps cases were absorbing
        # loops of 8+ identical payloads.
        self.loop_kill_repeats = loop_kill_repeats
        # Adaptive reasoning: when the agent reaches `escalate_at_step` without
        # self-terminating, bump the VLM's reasoning_effort. 0 = disabled.
        self.escalate_at_step = escalate_at_step
        self.escalate_to_effort = escalate_to_effort

    def run(self) -> TrajectoryResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if not self.goal_png.exists():
            raise FileNotFoundError(f"goal image not found: {self.goal_png}")

        env_inspector = EnvironmentInspector()
        env = env_inspector.inspect()
        freecad_bin = (
            self.freecad_binary
            or env_inspector.freecad_binary(env)
        )
        if not freecad_bin:
            raise RuntimeError("FreeCAD binary not found")

        logs_dir = self.output_dir / "logs"
        shots_dir = self.output_dir / "frames"
        logs_dir.mkdir(parents=True, exist_ok=True)
        shots_dir.mkdir(parents=True, exist_ok=True)

        display_mgr = self.display_manager or DisplayManager(logs_dir)
        session = display_mgr.acquire()
        owns_display = not session.reused

        video_path = self.output_dir / "trajectory.mp4"
        json_path = self.output_dir / "trajectory.json"

        freecad = FreeCADAutomation(
            display=session.display, logs_dir=logs_dir,
            freecad_binary=freecad_bin,
        )
        capture = ScreenshotCapture(shots_dir)

        # Wave-9 S2: write the document-state probe and point the executor at
        # it. After each python_eval the probe dumps the live ActiveDocument
        # object bboxes to agent_state.json, which we read back below to build
        # an AGENT_STATE hint for the next turn.
        state_json_path = self.output_dir / "agent_state.json"
        probe_path = self.output_dir / "_state_probe.py"
        probe_path.write_text(_STATE_PROBE_SRC.replace("__STATE_JSON__", str(state_json_path)))
        executor = ActionExecutor(state_probe_path=str(probe_path))

        recorder = ScreenRecorder(
            display=session.display,
            output=video_path,
            logs_dir=logs_dir,
        )

        steps: list[TrajectoryStep] = []
        terminated_by = "max_steps"
        error: str | None = None
        # Wave-9 S2: AGENT_STATE block built from the last python_eval's probe
        # readout, injected at the top of the next turn's history hint.
        agent_state_hint = ""
        system_prompt = SYSTEM_PROMPT_TMPL.format(action_space=ACTION_SPACE_SPEC)

        try:
            # Clear stale state before launch so the Document Recovery
            # dialog doesn't gate the first agent steps.
            self._reset_freecad_state()

            # Launch FreeCAD into its blank Start Page state.
            launch = freecad.launch(asset=None)
            time.sleep(self.freecad_post_launch_delay)

            # Start recording (this is T0; step 0's action_time is 0.0).
            handle = recorder.start()
            t0 = handle.start_monotonic

            # Step 0: null initialization.
            steps.append(TrajectoryStep(
                step_idx=0, action_time=0.0,
                action=None, rationale=None,
            ))

            for step_idx in range(1, self.max_steps + 1):
                # Adaptive reasoning escalation: if the agent has gotten this
                # far without self-terminating, it's likely stuck — bump the
                # reasoning effort so subsequent calls think harder.
                if (self.escalate_at_step
                        and step_idx > self.escalate_at_step
                        and self.vlm.reasoning_effort != self.escalate_to_effort):
                    prev = self.vlm.reasoning_effort
                    self.vlm.reasoning_effort = self.escalate_to_effort
                    print(f"[escalate] step {step_idx}: reasoning_effort "
                          f"{prev!r} -> {self.escalate_to_effort!r}",
                          flush=True)
                # Persist trajectory.json incrementally so a kill/crash mid-run
                # still leaves a usable record of all completed steps.
                self._write_json(json_path, steps, video_path=video_path,
                                 terminated_by="in_progress", error=None,
                                 model=self.vlm.model)
                current_png = shots_dir / f"step_{step_idx:02d}_before.png"
                shot = capture.capture(current_png.name)
                # ScreenshotCapture builds its own name under shots_dir; rename.
                if shot.path != current_png:
                    shot.path.rename(current_png)

                hist_hint = self._format_history(steps[-self.history_window:])
                # Wave-8 item 3: if recent history has >=2 python_evals
                # (excluding auto-frame injections) without a terminate, prepend
                # an anti-loop directive so the agent breaks out of the
                # build-then-build pattern instead of retrying identical code.
                recent_eval_codes = [
                    (s.action or {}).get("code")
                    for s in steps[-6:]
                    if (s.action or {}).get("type") == "python_eval"
                    and not (s.action or {}).get("_auto")
                ]
                if len(recent_eval_codes) >= 2 and recent_eval_codes[-1] == recent_eval_codes[-2]:
                    hist_hint = (
                        "ANTI-LOOP DIRECTIVE: Your last 2+ python_eval payloads "
                        "are IDENTICAL. The executor already framed the viewport "
                        "after each (via Gui.SendMsgToActiveView('ViewFit') + "
                        "frame keys). Re-emitting the same code AGAIN will not "
                        "create different geometry and will trigger loop-kill. "
                        "Your NEXT action MUST be one of:\n"
                        "  - {\"type\":\"terminate\"} if CURRENT_STATE matches GOAL_STATE\n"
                        "  - {\"type\":\"python_eval\",\"code\":...} with DIFFERENT code "
                        "(repair scale, add a missing feature, fix a wrong axis)\n"
                        "  - {\"type\":\"frame_view\"} only if you genuinely think the "
                        "viewport is still showing stale content\n"
                        "Do NOT re-emit the same python_eval code a third time.\n\n"
                        + hist_hint
                    )
                # Wave-9 S2/A2: prepend the document-state readout (+ stacking
                # repair recipe) from the previous build so it leads the hint.
                if agent_state_hint:
                    hist_hint = agent_state_hint + hist_hint
                try:
                    resp = self.vlm.next_action(
                        system_prompt=system_prompt,
                        goal_png=self.goal_png,
                        current_png=current_png,
                        step_idx=step_idx,
                        max_history_hint=hist_hint,
                    )
                except ValueError as exc:
                    # Parser couldn't extract a JSON action from the model's
                    # output. Log + continue — the agent may recover next turn.
                    steps.append(TrajectoryStep(
                        step_idx=step_idx,
                        action_time=time.monotonic() - t0,
                        action=None, rationale=None,
                        parse_error=f"parse: {exc}",
                    ))
                    continue
                except Exception as exc:  # noqa: BLE001 - transport / API
                    steps.append(TrajectoryStep(
                        step_idx=step_idx,
                        action_time=time.monotonic() - t0,
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
                        finish_reason=resp.finish_reason,
                        usage=resp.usage,
                    ))
                    terminated_by = "agent"
                    break

                exec_result = executor.execute(action) if isinstance(action, dict) else None
                if exec_result is None:
                    steps.append(TrajectoryStep(
                        step_idx=step_idx,
                        action_time=time.monotonic() - t0,
                        action=None, rationale=resp.rationale,
                        reasoning_trace=resp.reasoning_trace,
                        raw_content=resp.raw_content,
                        finish_reason=resp.finish_reason,
                        usage=resp.usage,
                        parse_error="action was not a JSON object",
                    ))
                    continue

                # Wait for the GUI to settle so the post-action frame is meaningful.
                time.sleep(max(exec_result.post_action_sleep, self.post_action_delay))
                after_t = time.monotonic() - t0
                steps.append(TrajectoryStep(
                    step_idx=step_idx,
                    action_time=after_t,
                    action=action,
                    rationale=resp.rationale,
                    reasoning_trace=resp.reasoning_trace,
                    raw_content=resp.raw_content,
                    finish_reason=resp.finish_reason,
                    usage=resp.usage,
                    exec_error=exec_result.error,
                ))
                # Wave-8: after a successful python_eval, inject a synthetic
                # frame_view step into the history. The python_eval executor
                # already does the framing (action_space.py items A+B), so
                # this is a HISTORY-ONLY record — no actual UI action runs.
                # Purpose: break the loop-detection signal (which counts
                # identical agent-emitted actions in `steps`) when the agent
                # repeats python_eval, AND surface the framing to the agent
                # in its history hint so it can choose `terminate` next.
                if (isinstance(action, dict)
                        and action.get("type") == "python_eval"
                        and exec_result.ok):
                    steps.append(TrajectoryStep(
                        step_idx=step_idx,  # share step_idx; no extra turn
                        action_time=after_t,
                        action={"type": "frame_view", "_auto": True},
                        rationale="(auto-injected: viewport was framed inside the python_eval executor)",
                    ))
                    # Wave-9 S2/A2: read back the document state the probe just
                    # wrote and build the AGENT_STATE hint for the next turn.
                    agent_state_hint = ""
                    try:
                        state = json.loads(state_json_path.read_text())
                        agent_state_hint = _build_agent_state_hint(state)
                    except (OSError, ValueError):
                        pass  # probe didn't write / malformed — skip this turn
                if not exec_result.ok:
                    # Don't terminate on a single bad action — let the agent
                    # observe the unchanged state and try again.
                    continue

                # Loop-kill: if the last N executed actions all canonicalise
                # to the same payload, the agent is in an absorbing state.
                # Force-terminate to save VLM cost and surface the failure
                # mode clearly (vs. silently hitting max_steps).
                if self.loop_kill_repeats >= 2:
                    tail = [json.dumps(s.action, sort_keys=True)
                            for s in steps[-self.loop_kill_repeats:]
                            if s.action is not None]
                    if (len(tail) == self.loop_kill_repeats
                            and len(set(tail)) == 1):
                        print(f"[loop-kill] step {step_idx}: identical action "
                              f"emitted {self.loop_kill_repeats}x in a row — terminating",
                              flush=True)
                        terminated_by = "agent_loop_detected"
                        break

            # Give the last action ~1s of video tail so its consequence is visible.
            time.sleep(1.0)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            terminated_by = "error"
        finally:
            try:
                recorder.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                freecad.quit()
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
        return TrajectoryResult(
            video_path=video_path, json_path=json_path,
            goal_path=self.goal_png, steps=steps,
            success=success, terminated_by=terminated_by, error=error,
        )

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _reset_freecad_state() -> None:
        """Pre-kill any FreeCAD process and wipe paths that would trigger
        the Document Recovery dialog on the next launch.

        FreeCAD 0.19 stores auto-recovery state under /tmp/FreeCAD_Doc_*/
        and a lockfile at /tmp/FreeCAD_<pid>.lock. Both must be removed
        for a clean launch.
        """
        # In parallel mode the orchestrator owns per-worker lifecycle and
        # this process MUST NOT kill sibling workers' FreeCAD instances.
        # `pkill -f freecad` is process-blind; skip it when the
        # orchestrator marks this run as parallel via CUA_WORKER_ID.
        # The HOME-isolated AutoRecovery dirs below are still cleared.
        in_parallel_mode = bool(os.environ.get("CUA_WORKER_ID"))
        if shutil.which("pkill") and not in_parallel_mode:
            subprocess.run(["pkill", "-9", "-f", "freecad"],
                           capture_output=True, timeout=5)
            time.sleep(1.5)
        # Static known paths.
        for path in (
            Path.home() / ".FreeCAD" / "AutoRecovery",
            Path.home() / ".config" / "FreeCAD" / "AutoRecovery",
            Path.home() / ".local" / "share" / "FreeCAD" / "AutoRecovery",
            Path("/tmp") / "FreeCAD-Crash",
        ):
            if path.exists():
                try:
                    shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
                except OSError:
                    pass
        # Dynamic per-PID dirs and lockfiles in /tmp.
        tmp = Path("/tmp")
        for child in tmp.glob("FreeCAD_Doc_*"):
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
        for child in tmp.glob("FreeCAD_*.lock"):
            try:
                child.unlink(missing_ok=True)
            except OSError:
                pass

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
