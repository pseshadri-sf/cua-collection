"""Blender-specific compound actions for the agent trajectory harness.

Blender 3.0 on Xvfb 1920x1080. Coordinates probed manually.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


# Standard Blender 3.0 workspace order. Index used by the workspace cycle.
BLENDER_WORKSPACES = (
    "Layout", "Modeling", "Sculpting", "UV Editing", "Texture Paint",
    "Shading", "Animation", "Rendering", "Compositing",
    "Geometry Nodes", "Scripting",
)

# Coordinates measured against bare Blender at 1920x1080, default theme.
TOPBAR_TAB_STRIP_XY = (600, 25)         # cursor must hover here for Ctrl+Prior/Next
SCRIPTING_PY_CONSOLE_XY = (200, 880)    # input prompt row in Scripting workspace
SCRIPTING_VIEWPORT_XY = (280, 200)      # 3D viewport centre in Scripting workspace
LAYOUT_VIEWPORT_XY = (700, 400)         # 3D viewport centre in Layout workspace


VALID_TYPES = {
    "move_to", "click", "double_click", "right_click",
    "type", "key", "hotkey", "scroll", "sleep", "terminate",
    "open_python_console", "switch_workspace",
    "focus_viewport",
    "python_eval", "frame_all",
}


ACTION_SPACE_SPEC = """\
You may emit exactly one action per turn, formatted as JSON. Available actions:

  {"type": "move_to", "x": int, "y": int}
      Move the cursor to (x,y). No click.

  {"type": "click", "x": int, "y": int}
      Move to (x,y) and left-click.

  {"type": "double_click", "x": int, "y": int}
      Move to (x,y) and double-click.

  {"type": "right_click", "x": int, "y": int}
      Move to (x,y) and right-click.

  {"type": "type", "text": "..."}
      Type the literal text at the current keyboard focus.

  {"type": "key", "key": "enter"}
      Press a single key. Useful keys for Blender: enter, escape, tab,
      backspace, delete, space, Home (frame all in viewport),
      KP_1/KP_3/KP_7 (front/right/top ortho), KP_5 (toggle persp/ortho),
      KP_0 (camera view).

  {"type": "hotkey", "keys": ["ctrl", "z"]}
      Chord: press keys together. Useful: ["ctrl","z"] undo, ["a"] select-all,
      ["x"] delete (in viewport), ["ctrl","Prior"] / ["ctrl","Next"]
      previous/next workspace.

  {"type": "scroll", "clicks": int}
      Scroll wheel. Positive = up, negative = down.

  {"type": "sleep", "seconds": float}
      Wait without sending any input.

  {"type": "terminate"}
      Stop the trajectory. Emit when CURRENT_STATE already matches GOAL_STATE.

  {"type": "open_python_console"}
      Compound macro: switches Blender to the Scripting workspace
      (which already contains a Python console docked at bottom-left)
      and clicks into the console's input row to give it keyboard focus.
      Use this as your first action when you want to construct geometry
      via bpy / bmesh. Equivalent to Window > Workspace > Scripting +
      a click in the console.

  {"type": "switch_workspace", "name": "Layout"}
      Switch to a specific Blender workspace by name. Supported names:
      Layout, Modeling, Sculpting, UV Editing, Texture Paint, Shading,
      Animation, Rendering, Compositing, Geometry Nodes, Scripting.
      Implemented as Ctrl+Prior/Next cycling (the only reliable method
      under Xvfb — workspace-tab clicks at the topbar don't register).

  {"type": "focus_viewport"}
      Compound macro: clicks in the 3D viewport area to transfer
      keyboard focus to it. ALWAYS issue this immediately before
      pressing viewport shortcuts (Home, Numpad keys) when the
      Python console or another panel might currently hold focus —
      otherwise the keystrokes get typed into that panel.
      In the Scripting workspace the viewport sits at (280, 200);
      in Layout it sits at roughly (700, 400). This macro targets
      the Scripting viewport since that's the workspace
      open_python_console leaves you in.

  {"type": "python_eval", "code": "<one line of Python>"}
      PREFERRED for all bpy actions. Compound macro that:
        1. Switches to the Scripting workspace if not already there.
        2. Clicks into the Python console input row (forces focus).
        3. Ctrl+A + Backspace clears any stray input.
        4. Types the code one character at a time.
        5. Presses Enter to execute.
        6. Sleeps so bpy ops finish and the viewport redraws.
      Atomic — focus can't drift between sub-steps. Always use this
      instead of chaining open_python_console + type + key("enter")
      manually.
      Code MUST be a single physical line. Multiple statements are
      OK if separated by semicolons, but `if`/`for`/`def` blocks
      with semicolons after the colon DO NOT WORK (Python rule:
      after `if x:` only one simple statement is allowed before
      semicolon). Use list comprehensions or rewrites that avoid
      block statements. Examples that work:
        import bpy; bpy.ops.mesh.primitive_monkey_add()
        import bpy; [bpy.data.objects.remove(o,do_unlink=True) for o in list(bpy.data.objects) if o.type=='MESH']
        import bpy; bpy.ops.object.select_all(action='DESELECT'); bpy.data.objects['Cube'].select_set(True); bpy.ops.object.delete()
      Examples that DO NOT work (broken Python):
        if l: l.name='Sun'; l.location=(4,-4,8)      # 2nd+3rd stmt run unconditionally!
        for o in bpy.data.objects: o.name='x'; o.hide=True   # 2nd stmt runs once after loop!

  {"type": "frame_all"}
      Compound macro: clicks the 3D viewport (transfers focus) then
      presses Home (Blender's "View All" shortcut). Atomic
      replacement for focus_viewport + key("Home").
"""


@dataclass
class ExecutionResult:
    ok: bool
    error: str | None = None
    post_action_sleep: float = 0.5


class BlenderActionExecutor:
    """Validates and executes one action via pyautogui + xdotool.

    Mirrors ActionExecutor but with Blender-specific compound actions
    (Python-console toggle, workspace switching, viewport focus).
    """

    def __init__(self, display: str = ":99"):
        import pyautogui  # noqa: PLC0415
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.05
        self._pg = pyautogui
        self.display = display
        # Tracks current workspace so switch_workspace knows how far to cycle.
        self._current_workspace = "Layout"

    def execute(self, action: dict[str, Any]) -> ExecutionResult:
        if not isinstance(action, dict) or "type" not in action:
            return ExecutionResult(False, "action must be an object with a 'type' field")
        t = action["type"]
        if t not in VALID_TYPES:
            return ExecutionResult(False, f"unknown action type: {t!r}")
        try:
            return getattr(self, f"_do_{t}")(action)
        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(False, f"{type(exc).__name__}: {exc}")

    # --- basic handlers (mirror ActionExecutor) ----------------------------

    def _do_move_to(self, a: dict) -> ExecutionResult:
        x, y = self._xy(a)
        self._pg.moveTo(x, y, duration=0.15)
        return ExecutionResult(True, post_action_sleep=0.2)

    def _do_click(self, a: dict) -> ExecutionResult:
        x, y = self._xy(a)
        self._pg.moveTo(x, y, duration=0.15)
        self._pg.click()
        return ExecutionResult(True, post_action_sleep=0.6)

    def _do_double_click(self, a: dict) -> ExecutionResult:
        x, y = self._xy(a)
        self._pg.moveTo(x, y, duration=0.15)
        self._pg.doubleClick()
        return ExecutionResult(True, post_action_sleep=0.8)

    def _do_right_click(self, a: dict) -> ExecutionResult:
        x, y = self._xy(a)
        self._pg.moveTo(x, y, duration=0.15)
        self._pg.rightClick()
        return ExecutionResult(True, post_action_sleep=0.6)

    def _do_type(self, a: dict) -> ExecutionResult:
        text = a.get("text")
        if not isinstance(text, str):
            return ExecutionResult(False, "'type' requires string 'text'")
        self._pg.typewrite(text, interval=0.01)
        return ExecutionResult(True, post_action_sleep=0.4)

    def _do_key(self, a: dict) -> ExecutionResult:
        key = a.get("key")
        if not isinstance(key, str) or not key:
            return ExecutionResult(False, "'key' requires non-empty string 'key'")
        self._pg.press(key)
        return ExecutionResult(True, post_action_sleep=0.5)

    def _do_hotkey(self, a: dict) -> ExecutionResult:
        keys = a.get("keys")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            return ExecutionResult(False, "'hotkey' requires non-empty list 'keys' of strings")
        self._pg.hotkey(*keys)
        return ExecutionResult(True, post_action_sleep=0.6)

    def _do_scroll(self, a: dict) -> ExecutionResult:
        clicks = a.get("clicks")
        if not isinstance(clicks, (int, float)):
            return ExecutionResult(False, "'scroll' requires numeric 'clicks'")
        self._pg.scroll(int(clicks))
        return ExecutionResult(True, post_action_sleep=0.4)

    def _do_sleep(self, a: dict) -> ExecutionResult:
        seconds = a.get("seconds", 1.0)
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return ExecutionResult(False, "'sleep' requires numeric 'seconds'")
        seconds = max(0.0, min(seconds, 10.0))
        time.sleep(seconds)
        return ExecutionResult(True, post_action_sleep=0.0)

    def _do_terminate(self, a: dict) -> ExecutionResult:
        return ExecutionResult(True, post_action_sleep=0.0)

    # --- Blender-specific compound actions ---------------------------------

    def _do_open_python_console(self, a: dict) -> ExecutionResult:
        """Cycle to the Scripting workspace + click console input."""
        self._cycle_to("Scripting")
        time.sleep(1.0)
        x, y = SCRIPTING_PY_CONSOLE_XY
        self._pg.moveTo(x, y, duration=0.15)
        self._pg.click()
        return ExecutionResult(True, post_action_sleep=0.8)

    def _do_switch_workspace(self, a: dict) -> ExecutionResult:
        name = a.get("name")
        if name not in BLENDER_WORKSPACES:
            return ExecutionResult(
                False,
                f"unknown workspace {name!r}; supported: {list(BLENDER_WORKSPACES)}",
            )
        self._cycle_to(name)
        return ExecutionResult(True, post_action_sleep=1.5)

    def _do_focus_viewport(self, a: dict) -> ExecutionResult:
        """Click viewport in the current workspace.

        Uses Scripting coords by default (since the agent typically
        invokes this after open_python_console). For Layout workspace
        the caller should explicitly use a `click` action.
        """
        x, y = SCRIPTING_VIEWPORT_XY if self._current_workspace == "Scripting" else LAYOUT_VIEWPORT_XY
        self._pg.moveTo(x, y, duration=0.15)
        self._pg.click()
        return ExecutionResult(True, post_action_sleep=0.5)

    def _do_python_eval(self, a: dict) -> ExecutionResult:
        """Atomic: switch to Scripting, focus console, type, Enter, settle."""
        code = a.get("code")
        if not isinstance(code, str) or not code.strip():
            return ExecutionResult(False, "'python_eval' requires non-empty string 'code'")
        if self._current_workspace != "Scripting":
            self._cycle_to("Scripting")
            time.sleep(1.0)
        cx, cy = SCRIPTING_PY_CONSOLE_XY
        self._pg.moveTo(cx, cy, duration=0.15)
        self._pg.click()
        time.sleep(0.4)
        self._pg.hotkey("ctrl", "a")
        time.sleep(0.1)
        self._pg.press("backspace")
        time.sleep(0.1)
        self._pg.typewrite(code, interval=0.005)
        time.sleep(0.3)
        self._pg.press("enter")
        time.sleep(1.5)
        return ExecutionResult(True, post_action_sleep=0.4)

    def _do_frame_all(self, a: dict) -> ExecutionResult:
        """Atomic: click viewport (focus) + press Home (Frame All)."""
        x, y = SCRIPTING_VIEWPORT_XY if self._current_workspace == "Scripting" else LAYOUT_VIEWPORT_XY
        self._pg.moveTo(x, y, duration=0.15)
        self._pg.click()
        time.sleep(0.3)
        self._pg.press("Home")
        time.sleep(0.5)
        return ExecutionResult(True, post_action_sleep=0.4)

    # --- internals ---------------------------------------------------------

    def _cycle_to(self, target: str) -> None:
        cur_idx = BLENDER_WORKSPACES.index(self._current_workspace)
        tgt_idx = BLENDER_WORKSPACES.index(target)
        n = len(BLENDER_WORKSPACES)
        forward = (tgt_idx - cur_idx) % n
        backward = (cur_idx - tgt_idx) % n
        # Cursor must be over a workspace-aware area for the shortcut to fire.
        tx, ty = TOPBAR_TAB_STRIP_XY
        self._pg.moveTo(tx, ty, duration=0.15)
        time.sleep(0.3)
        # pyautogui uses "pageup"/"pagedown" (NOT X11 names "Prior"/"Next");
        # the X11 names silently no-op, leaving the workspace unchanged.
        if forward <= backward:
            for _ in range(forward):
                self._pg.hotkey("ctrl", "pagedown")
                time.sleep(0.25)
        else:
            for _ in range(backward):
                self._pg.hotkey("ctrl", "pageup")
                time.sleep(0.25)
        self._current_workspace = target

    @staticmethod
    def _xy(a: dict) -> tuple[int, int]:
        x, y = a.get("x"), a.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise ValueError("integer 'x' and 'y' required")
        return int(x), int(y)
