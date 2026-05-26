"""End-to-end agentic trajectory runner.

Bootstraps:
  - acquires an X display (via DisplayManager from cua_smoketest.display)
  - launches FreeCAD into a blank state (no asset)
  - starts an ffmpeg screen recording of the whole display
  - loops at most `max_steps` times:
      * captures the current display as PNG
      * asks the VLM for the next action given (system, goal, current)
      * executes the action via pyautogui
      * records the post-action timestamp as the step's `action_time`
  - terminates on `terminate` action or step exhaustion
  - writes a JSON trajectory log and returns paths to video + JSON
"""
from __future__ import annotations

import json
import os
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

Recommended approaches (pick whichever fits the GOAL_STATE best):

A) Part workbench primitive (best for simple shapes like cubes, cylinders,
   spheres, cones, toruses):
   1. FreeCAD opens in the "Start" workbench. The workbench selector
      dropdown is on the main toolbar around (500, 40). Click it,
      then click "Part" in the dropdown list.
   2. Once Part workbench is active, a "Part" menu appears in the
      menubar near (180, 10). Open it and choose Primitives > Box
      (or Cylinder, Sphere, Cone, Torus).
   3. Alternatively click the Box icon in the Part toolbar (toolbar
      icons appear near y=80 after switching to Part workbench).

B) Python console (most deterministic, recommended if menu clicks fail):
   1. Open via View > Panels > Python console; this docks a console at
      the bottom of the window. The View menu is near (100, 10).
   2. Click into the console input area near (700, 1000) to focus it,
      then type a one-line Python snippet to build the geometry. For
      a box, type for example:
      doc = App.newDocument(); import Part; b = Part.makeBox(50, 70, 30); o = doc.addObject('Part::Feature', 'Box'); o.Shape = b; doc.recompute()
   3. Press Enter to execute.

C) After the geometry exists, frame the camera so it matches GOAL_STATE:
   press the key "0" for isometric view, then press "v" followed by "f"
   for "Fit All". Note: V,F keystrokes only work when the cursor is over
   the 3D viewport area, so move the cursor to roughly (1100, 540) first.

Other tips:
  - You can create a new document with hotkey Ctrl+N. FreeCAD's Start
    page is informational only and contains no geometry.
  - Software OpenGL is slow; after creating geometry or switching
    workbenches, give the GUI 1-2 seconds to repaint by issuing a
    sleep action before screenshotting.
  - If CURRENT_STATE already visually matches GOAL_STATE (a recognisable
    3D shape in the viewport matching the goal's shape, with a similar
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
                 max_steps: int = 25,
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
                    ))
                    terminated_by = "agent"
                    break

                exec_result = executor.execute(action) if isinstance(action, dict) else None
                if exec_result is None:
                    steps.append(TrajectoryStep(
                        step_idx=step_idx,
                        action_time=time.monotonic() - t0,
                        action=None, rationale=resp.rationale,
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
                "parse_error": s.parse_error,
                "exec_error": s.exec_error,
            })
        json_path.write_text(json.dumps(payload, indent=2))
