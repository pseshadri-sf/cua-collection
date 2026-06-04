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
from .runner import _CompositionalDone


# Run at Blender launch (--python). Forces every 3D viewport to flat, bright
# SOLID shading on a dark background so geometry is clearly visible under Mesa
# software GL (default studio+grey shading renders near-black). Wrapped in a
# timer so it runs after the UI is fully built.
_BRIGHT_VIEWPORT_SRC = '''\
import bpy
def _apply():
    for scr in bpy.data.screens:
        for area in scr.areas:
            if area.type != "VIEW_3D":
                continue
            for sp in area.spaces:
                if sp.type != "VIEW_3D":
                    continue
                sh = sp.shading
                sh.type = "SOLID"
                sh.light = "FLAT"
                sh.color_type = "SINGLE"
                sh.single_color = (0.85, 0.88, 1.0)
                sh.background_type = "VIEWPORT"
                sh.background_color = (0.08, 0.08, 0.10)
    return None
bpy.app.timers.register(_apply, first_interval=0.5)
_apply()
'''


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
  Step 1: {{"type":"python_eval","code":"import bpy; bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(); bpy.ops.mesh.primitive_monkey_add()"}}
         -- Atomic: switches to Scripting, focuses console, types
         the line, presses Enter, and waits for execution. ALWAYS
         use python_eval instead of chaining open_python_console +
         type + key("enter") manually -- focus drift between those
         three actions is the #1 failure mode here.
         Replace primitive_monkey_add(...) with whatever GOAL_STATE shows.
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
  Step 2: {{"type":"frame_all"}}
         -- Atomic: clicks viewport (transfers focus) then presses
         Home to fit all geometry. Equivalent of focus_viewport +
         key("Home") but reliable.
  Step 3: terminate if visual match is satisfactory.

CRITICAL RULES for python_eval `code`:
  - Must be ONE physical line.
  - Multiple statements via semicolons are OK in straight-line code.
  - BUT `if cond: stmt1; stmt2` does NOT mean "do stmt1 and stmt2 if
    cond" -- only stmt1 is conditional, stmt2 always runs. Same for
    for/while bodies. If you need multiple statements under a
    condition, use a list comprehension or split across multiple
    python_eval actions.
  - Use list comprehensions instead of loops:
      [bpy.data.objects.remove(o,do_unlink=True) for o in list(bpy.data.objects) if o.type=='MESH']

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
                 history_window: int = 10,
                 # Wave-8: bump 3->5, parallel to FC runner. BL doesn't
                 # auto-inject frame_all (BL python_eval already updates
                 # viewport correctly in most cases), but the extra retries
                 # still help on edge cases.
                 loop_kill_repeats: int = 5,
                 escalate_at_step: int = 0,
                 escalate_to_effort: str = "high",
                 planner_model: str | None = None,
                 plan_format: str = "python_eval",
                 bright_viewport: bool = False,
                 planner_reasoning: str = "low",
                 compositional: bool = False):
        self.compositional = compositional
        self.planner_reasoning = planner_reasoning
        self.bright_viewport = bright_viewport
        self.goal_png = Path(goal_png).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.vlm = vlm
        self.max_steps = max_steps
        self.display_manager = display_manager
        self.blender_binary = blender_binary
        self.blender_post_launch_delay = blender_post_launch_delay
        self.post_action_delay = post_action_delay
        self.history_window = history_window
        # Loop-kill: terminate when N consecutive identical action payloads
        # are emitted. See AgentTrajectoryRunner for sweep120 diagnosis.
        self.loop_kill_repeats = loop_kill_repeats
        # Adaptive reasoning: bump VLM reasoning_effort once the agent has
        # taken `escalate_at_step` steps without self-terminating. 0 = off.
        self.escalate_at_step = escalate_at_step
        self.escalate_to_effort = escalate_to_effort
        self.planner_model = planner_model
        self.plan_format = plan_format

    def _run_compositional(self, plan, executor, capture, shots_dir, steps, t0,
                           json_path, video_path):
        """Replay the plan's per-component bpy steps, one python_eval each, so
        the video shows the scene built object-by-object. Returns terminated_by."""
        plan_steps = [s for s in plan.get("steps", []) if s.get("code")]
        print(f"[compositional] BL replaying {len(plan_steps)} component steps", flush=True)
        for i, st in enumerate(plan_steps, start=1):
            self._write_json(json_path, steps, video_path=video_path,
                             terminated_by="in_progress", error=None,
                             model=self.vlm.model)
            action = {"type": "python_eval", "code": st["code"]}
            res = executor.execute(action)
            time.sleep(max(res.post_action_sleep, self.post_action_delay))
            png = shots_dir / f"step_{i:02d}_after.png"
            shot = capture.capture(png.name)
            if shot.path != png:
                shot.path.rename(png)
            steps.append(TrajectoryStep(
                step_idx=i, action_time=time.monotonic() - t0, action=action,
                rationale=f"component {i}/{len(plan_steps)}: "
                          f"{st.get('name','')} — {st.get('why','')}",
                exec_error=res.error,
            ))
            time.sleep(1.0)
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

        # frontier-onepass Variant A (Blender): one frontier call up front →
        # a bpy BUILD_PLAN injected as guidance every turn. Best-effort.
        plan_block = ""
        plan_obj = None
        if self.planner_model:
            try:
                from .frontier_planner import FrontierPlanner, render_plan_block
                from .vlm_client import _extract_goal_name
                planner = FrontierPlanner(api_key=self.vlm.api_key,
                                          model=self.planner_model, app="blender",
                                          image_max_dim=self.vlm.image_max_dim,
                                          reasoning_effort=self.planner_reasoning,
                                          compositional=self.compositional)
                plan = planner.plan(self.goal_png, goal_name=_extract_goal_name(self.goal_png))
                if plan:
                    plan_obj = plan
                    (self.output_dir / "build_plan.json").write_text(json.dumps(plan, indent=2))
                    plan_block = render_plan_block(plan, self.plan_format)
                    print(f"[planner] BL plan ready: {len(plan.get('steps', []))} steps "
                          f"compositional={self.compositional}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[planner] BL skipped ({type(exc).__name__}: {exc})", flush=True)

        # Optional: force a flat, bright SOLID viewport so the agent (which sees
        # the live viewport as CURRENT_STATE) and our screenshots can actually
        # see the built geometry. Default Blender solid+studio shading renders
        # near-black under Mesa software GL. Set once at launch; persists across
        # the agent's select_all/delete rebuilds (it's a space property).
        startup_py = None
        if self.bright_viewport or self.compositional:  # bright helps the build video
            startup_py = self.output_dir / "_bright_viewport.py"
            startup_py.write_text(_BRIGHT_VIEWPORT_SRC)

        try:
            self._reset_blender_state()
            launch = blender.launch(asset=None, startup_py=startup_py)
            time.sleep(self.blender_post_launch_delay)
            # Dismiss the launch splash that Blender always shows.
            blender.dismiss_splash()
            time.sleep(0.5)

            handle = recorder.start()
            t0 = handle.start_monotonic

            steps.append(TrajectoryStep(
                step_idx=0, action_time=0.0, action=None, rationale=None,
            ))

            # compositional_dynamics (Blender): deterministically replay the
            # plan's per-component bpy steps, one python_eval each, so the video
            # shows the asset built object-by-object.
            if self.compositional and plan_obj and plan_obj.get("steps"):
                terminated_by = self._run_compositional(
                    plan_obj, executor, capture, shots_dir, steps, t0, json_path,
                    video_path)
                raise _CompositionalDone()

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
                if shot.path != current_png:
                    shot.path.rename(current_png)

                hist_hint = self._format_history(steps[-self.history_window:])
                # Wave-8 item 3: anti-loop directive when last 2 python_evals
                # are identical (excluding auto-injected steps).
                recent_eval_codes = [
                    (s.action or {}).get("code")
                    for s in steps[-6:]
                    if (s.action or {}).get("type") == "python_eval"
                    and not (s.action or {}).get("_auto")
                ]
                if len(recent_eval_codes) >= 2 and recent_eval_codes[-1] == recent_eval_codes[-2]:
                    hist_hint = (
                        "ANTI-LOOP DIRECTIVE: Your last 2+ python_eval payloads "
                        "are IDENTICAL. The Blender viewport already shows the "
                        "result of the previous execution. Re-emitting the same "
                        "code will trigger loop-kill at the 3rd identical attempt. "
                        "Your NEXT action MUST be one of:\n"
                        "  - {\"type\":\"terminate\"} if CURRENT_STATE matches GOAL_STATE\n"
                        "  - {\"type\":\"python_eval\",\"code\":...} with DIFFERENT code "
                        "(repair scale, add a missing primitive, fix axis)\n"
                        "  - {\"type\":\"frame_all\"} if the viewport is still showing "
                        "stale geometry off-screen\n"
                        "Do NOT re-emit the same python_eval a third time.\n\n"
                        + hist_hint
                    )
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
                # Wave-8.1 REVERT: the synthetic frame_all injection (parallel
                # to the FC runner change) was removed for Blender. The full
                # Wave-8 benchmark showed it REGRESSED BL: interleaving
                # py / frame_all / py / frame_all defeats the identical-in-a-row
                # loop-kill check below, so BL agents kept overwriting a correct
                # first build with worse retries instead of being force-terminated.
                # FC benefits from the injection (viewport-staleness loop); BL
                # does not, so the two runners now differ here. The loop_kill
                # bump (3->5) and the anti-loop directive are retained for BL.

                # Loop-kill: same payload N times in a row → force-terminate.
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

            time.sleep(1.0)
        except _CompositionalDone:
            pass  # compositional replay finished; terminated_by already set
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
        # IMPORTANT: don't use pkill -f "blender" — that matches our own
        # process (blender_agent_trajectory.py) by command-line. Match only
        # the Blender binary basename via pgrep -x and kill by PID.
        # In parallel mode the orchestrator owns per-worker lifecycle and
        # this process MUST NOT kill sibling workers' Blender instances.
        in_parallel_mode = bool(os.environ.get("CUA_WORKER_ID"))
        if shutil.which("pgrep") and shutil.which("kill") and not in_parallel_mode:
            res = subprocess.run(
                ["pgrep", "-x", "blender"],
                capture_output=True, text=True, timeout=5,
            )
            for pid in res.stdout.split():
                try:
                    subprocess.run(["kill", "-9", pid],
                                   capture_output=True, timeout=5)
                except subprocess.SubprocessError:
                    pass
            time.sleep(1.5)
        # Blender keeps autosave .blend in /tmp/quit.blend on quit; remove it.
        for path in (Path("/tmp/quit.blend"),):
            if path.exists():
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
