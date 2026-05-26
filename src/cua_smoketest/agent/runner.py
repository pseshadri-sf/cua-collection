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
  Step 1: click workbench selector at (550, 40)         -> dropdown opens
  Step 2: click "Part" in the dropdown (around (550,200))
  Step 3: click the Part menu in the menubar at (~220, 10)
  Step 4: hover "Primitives" submenu, click "Box"        -> box appears
  Step 5: move cursor to viewport (1100, 540), press "0" then "v","f"

=== STRATEGY B: Python console (most reliable; recommended) ===
  Step 1: click View menu at (99, 10)                  -> dropdown
  Step 2: in the View dropdown, hover "Panels"          -> submenu
  Step 3: click "Python console" in the Panels submenu  -> docked at bottom
  Step 4: click the console's input field, somewhere around (700, 990)
  Step 5: type a one-liner. For a box:
            doc=App.newDocument();import Part;b=Part.makeBox(50,70,30);o=doc.addObject('Part::Feature','Box');o.Shape=b;doc.recompute()
  Step 6: press Enter
  Step 7: move cursor to viewport (1100, 540), press "0" then "v","f"

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


class AgentTrajectoryRunner:
    def __init__(self, *, goal_png: Path, output_dir: Path,
                 vlm: OpenRouterVLMClient,
                 max_steps: int = 15,
                 display_manager: DisplayManager | None = None,
                 freecad_binary: str | None = None,
                 freecad_post_launch_delay: float = 4.0,
                 post_action_delay: float = 0.6,
                 history_window: int = 4):
        self.goal_png = Path(goal_png).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.vlm = vlm
        self.max_steps = max_steps
        self.display_manager = display_manager
        self.freecad_binary = freecad_binary
        self.freecad_post_launch_delay = freecad_post_launch_delay
        self.post_action_delay = post_action_delay
        self.history_window = history_window

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
        executor = ActionExecutor()

        recorder = ScreenRecorder(
            display=session.display,
            output=video_path,
            logs_dir=logs_dir,
        )

        steps: list[TrajectoryStep] = []
        terminated_by = "max_steps"
        error: str | None = None
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
                current_png = shots_dir / f"step_{step_idx:02d}_before.png"
                shot = capture.capture(current_png.name)
                # ScreenshotCapture builds its own name under shots_dir; rename.
                if shot.path != current_png:
                    shot.path.rename(current_png)

                hist_hint = self._format_history(steps[-self.history_window:])
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
                if not exec_result.ok:
                    # Don't terminate on a single bad action — let the agent
                    # observe the unchanged state and try again.
                    continue

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
        the Document Recovery dialog on the next launch."""
        if shutil.which("pkill"):
            subprocess.run(["pkill", "-9", "-f", "freecad"],
                           capture_output=True, timeout=5)
            time.sleep(1.5)
        for path in (
            Path.home() / ".FreeCAD" / "AutoRecovery",
            Path.home() / ".config" / "FreeCAD" / "AutoRecovery",
            Path.home() / ".local" / "share" / "FreeCAD" / "AutoRecovery",
            Path("/tmp") / "FreeCAD-Crash",
        ):
            if path.exists():
                try:
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)
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
