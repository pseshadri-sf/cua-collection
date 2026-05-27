"""Agentic trajectory runner for Blender.

Same shape as AgentTrajectoryRunner but launches Blender (not FreeCAD)
and uses BlenderActionExecutor + a Blender-specific system prompt.
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

from ..blender_automation import BlenderAutomation
from ..display import DisplayManager, DisplaySession
from ..environment import EnvironmentInspector
from ..screenshots import ScreenshotCapture
from .blender_action_space import ACTION_SPACE_SPEC, BlenderActionExecutor
from .screen_recorder import ScreenRecorder
from .vlm_client import OpenRouterVLMClient


SYSTEM_PROMPT_TMPL = """You are an autonomous GUI agent controlling Blender 3.0 \
on a Linux desktop (1920x1080) via pyautogui.

TASK: Visually recreate the GOAL_STATE in Blender by CONSTRUCTING the
geometry from scratch. You are NOT allowed to load existing files —
no File > Open, File > Recent, drag-and-drop, or "Append/Link"
operations. Build everything using bpy primitives, modifiers, or the
3D-viewport menus.

{action_space}

=== UI LANDMARKS (Blender 3.0, 1920x1080, default theme) ===

Topbar workspace tabs (y around 25 but clicking tabs is unreliable on
Xvfb — USE the `switch_workspace` macro instead of clicking tabs):
  Layout, Modeling, Sculpting, UV Editing, Texture Paint, Shading,
  Animation, Rendering, Compositing, Geometry Nodes, Scripting.

In the Layout workspace (the default):
  3D viewport occupies most of the centre; centre at (700, 400).
  Outliner top-right.
  Properties panel bottom-right.
  Default scene has a Cube, Camera, and Light at the origin.

In the Scripting workspace:
  3D viewport top-left, centre at (280, 200).
  Python console bottom-left, input prompt around (200, 880).
  Text editor right.

=== STRATEGY: Python console (recommended) ===
  Step 1: {{"type":"open_python_console"}}
         -- single macro: cycles Layout -> Scripting and focuses
         the console input row.
  Step 2: {{"type":"type","text":"<bpy one-liner>"}}
         A good template for replacing the default cube and adding new
         geometry, all on one line:
           import bpy; bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(); bpy.ops.mesh.primitive_cube_add(size=2)
         Replace primitive_cube_add(...) with whatever GOAL_STATE shows.
         Useful primitives (all in bpy.ops.mesh.*):
           primitive_cube_add(size=2)
           primitive_uv_sphere_add(radius=1)
           primitive_ico_sphere_add(subdivisions=2, radius=1)
           primitive_cylinder_add(radius=1, depth=2)
           primitive_cone_add(radius1=1, depth=2)
           primitive_torus_add(major_radius=1, minor_radius=0.25)
           primitive_monkey_add()    # Suzanne
           primitive_plane_add(size=2)
         All take location=(x,y,z) and rotation=(rx,ry,rz) kwargs.
  Step 3: {{"type":"key","key":"enter"}}  -- execute the line.
  Step 4: {{"type":"focus_viewport"}}     -- transfer focus to 3D view.
         CRITICAL: console keeps focus after Enter; Home and other
         viewport shortcuts will be TYPED INTO THE CONSOLE without
         this step.
  Step 5: {{"type":"key","key":"Home"}}   -- "Frame All": zooms the
         camera to fit all visible geometry. This is Blender's
         equivalent of FreeCAD's V,F.
  Step 6: terminate if visual match is satisfactory.

=== STRATEGY: 3D viewport menus (fallback) ===
  Move cursor over viewport, press Shift+A to open the Add menu.
  Navigate Mesh > <Primitive>. Each insertion follows the cursor.
  Use this only if Python console isn't cooperating.

=== HOUSEKEEPING ===
  - Blender always launches with a default Cube + Camera + Light.
    For most goals you'll want to delete the default cube first
    (one-line Python: bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete()).
  - If a splash overlay appears at launch, press {{"type":"key","key":"escape"}} to dismiss.
  - Software OpenGL is slow; after creating geometry insert a
    {{"type":"sleep","seconds":1.5}} action before screenshotting.
  - If CURRENT_STATE already visually matches GOAL_STATE (a
    recognisable 3D shape in the viewport, similar camera framing),
    emit {{"action": {{"type": "terminate"}}, ...}}.

Output: exactly one JSON object per turn:
  {{"action": <action>, "rationale": "<one or two sentences>"}}
"""


@dataclass
class TrajectoryStep:
    step_idx: int
    action_time: float
    action: dict | None
    rationale: str | None
    reasoning_trace: str | None = None
    raw_content: str | None = None
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
    terminated_by: str
    error: str | None = None


class BlenderAgentTrajectoryRunner:
    def __init__(self, *, goal_png: Path, output_dir: Path,
                 vlm: OpenRouterVLMClient,
                 max_steps: int = 15,
                 display_manager: DisplayManager | None = None,
                 blender_binary: str | None = None,
                 blender_post_launch_delay: float = 8.0,
                 post_action_delay: float = 0.6,
                 history_window: int = 4):
        self.goal_png = Path(goal_png).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.vlm = vlm
        self.max_steps = max_steps
        self.display_manager = display_manager
        self.blender_binary = blender_binary
        self.blender_post_launch_delay = blender_post_launch_delay
        self.post_action_delay = post_action_delay
        self.history_window = history_window

    def run(self) -> TrajectoryResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if not self.goal_png.exists():
            raise FileNotFoundError(f"goal image not found: {self.goal_png}")

        env_inspector = EnvironmentInspector()
        env = env_inspector.inspect()
        blender_bin = (
            self.blender_binary
            or env.tools.get("blender")
            or shutil.which("blender")
        )
        if not blender_bin:
            raise RuntimeError("Blender binary not found")

        logs_dir = self.output_dir / "logs"
        shots_dir = self.output_dir / "frames"
        logs_dir.mkdir(parents=True, exist_ok=True)
        shots_dir.mkdir(parents=True, exist_ok=True)

        display_mgr = self.display_manager or DisplayManager(logs_dir)
        session = display_mgr.acquire()
        owns_display = not session.reused

        video_path = self.output_dir / "trajectory.mp4"
        json_path = self.output_dir / "trajectory.json"

        blender = BlenderAutomation(
            display=session.display, logs_dir=logs_dir,
            blender_binary=blender_bin,
        )
        capture = ScreenshotCapture(shots_dir)
        executor = BlenderActionExecutor(display=session.display)
        recorder = ScreenRecorder(
            display=session.display, output=video_path, logs_dir=logs_dir,
        )

        steps: list[TrajectoryStep] = []
        terminated_by = "max_steps"
        error: str | None = None
        system_prompt = SYSTEM_PROMPT_TMPL.format(action_space=ACTION_SPACE_SPEC)

        try:
            self._reset_blender_state()
            launch = blender.launch(asset=None)
            time.sleep(self.blender_post_launch_delay)
            # Dismiss the launch splash that Blender always shows.
            blender.dismiss_splash()
            time.sleep(0.5)

            handle = recorder.start()
            t0 = handle.start_monotonic

            steps.append(TrajectoryStep(
                step_idx=0, action_time=0.0, action=None, rationale=None,
            ))

            for step_idx in range(1, self.max_steps + 1):
                current_png = shots_dir / f"step_{step_idx:02d}_before.png"
                shot = capture.capture(current_png.name)
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
                    steps.append(TrajectoryStep(
                        step_idx=step_idx,
                        action_time=time.monotonic() - t0,
                        action=None, rationale=None,
                        parse_error=f"parse: {exc}",
                    ))
                    continue
                except Exception as exc:  # noqa: BLE001
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
                blender.quit()
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
    def _reset_blender_state() -> None:
        if shutil.which("pkill"):
            subprocess.run(["pkill", "-9", "-f", "blender"],
                           capture_output=True, timeout=5)
            time.sleep(1.5)
        # Blender keeps autosave .blend in /tmp/quit.blend on quit; remove it.
        for path in (Path("/tmp/quit.blend"), Path.home() / ".config" / "blender"):
            # The blender config dir is preserved; only clear the temp save.
            if path.name == "quit.blend" and path.exists():
                try:
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
