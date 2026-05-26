"""Action space for the agentic trajectory harness.

Every action the VLM can emit is a JSON object with a `type` field. The
executor parses it and dispatches to pyautogui. Coordinates are screen
pixels (0,0 = top-left, 1920x1080 default).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


VALID_TYPES = {
    "move_to", "click", "double_click", "right_click",
    "type", "key", "hotkey", "scroll", "sleep", "terminate",
    "menu_navigate", "switch_workbench", "focus_viewport",
}


# Where to click to give the 3D viewport keyboard focus on Xvfb/Qt.
VIEWPORT_FOCUS_XY: tuple[int, int] = (1100, 540)


# Pre-measured click/hover sequences for FreeCAD 0.19 nested menus on
# 1920x1080. Each entry is a list of ("click"|"hover", x, y, post_delay).
# The "hover" step uses pyautogui.moveTo only (no click) so Qt opens the
# child submenu on cursor entry; the leaf must be a "click".
MENU_PATHS: dict[tuple[str, ...], list[tuple[str, int, int, float]]] = {
    ("View", "Panels", "Report view"): [
        ("click", 99, 10, 0.8),
        ("hover", 130, 543, 0.5),
        ("hover", 260, 543, 0.8),
        ("click", 510, 552, 0.6),
    ],
    ("View", "Panels", "Selection view"): [
        ("click", 99, 10, 0.8),
        ("hover", 130, 543, 0.5),
        ("hover", 260, 543, 0.8),
        ("click", 510, 576, 0.6),
    ],
    ("View", "Panels", "Combo View"): [
        ("click", 99, 10, 0.8),
        ("hover", 130, 543, 0.5),
        ("hover", 260, 543, 0.8),
        ("click", 510, 600, 0.6),
    ],
    ("View", "Panels", "Python console"): [
        ("click", 99, 10, 0.8),
        ("hover", 130, 543, 0.5),
        ("hover", 260, 543, 0.8),
        ("click", 515, 616, 0.6),
    ],
    ("Part", "Primitives", "Box"): [
        # Part menu only appears after switching to the Part workbench.
        # Coordinates here are best-effort; if click on Box fails the
        # agent should fall back to the Python console approach.
        ("click", 220, 10, 0.8),
        ("hover", 260, 240, 0.5),
        ("hover", 380, 240, 0.8),
        ("click", 500, 240, 0.6),
    ],
}

# Pre-measured coords for the workbench-selector dropdown items.
WORKBENCH_COORDS: dict[str, tuple[int, int, int, int]] = {
    # name: (dropdown_btn_x, dropdown_btn_y, item_x, item_y)
    "Part":        (550, 40, 510, 252),
    "Start":       (550, 40, 510, 444),
    "Sketcher":    (550, 40, 510, 396),
    "Part Design": (550, 40, 510, 228),
    "Draft":       (550, 40, 510, 36),
    "Mesh Design": (550, 40, 510, 156),
}


# Authoritative human-readable spec embedded in the system prompt.
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
      Press a single key. Valid keys: enter, escape, tab, backspace, delete,
      space, up, down, left, right, home, end, pageup, pagedown,
      f1..f12, or any single character.

  {"type": "hotkey", "keys": ["ctrl", "o"]}
      Chord: press all keys together, release together. Common: ["ctrl","o"],
      ["ctrl","a"], ["ctrl","s"], ["alt","f4"].

  {"type": "scroll", "clicks": int}
      Scroll wheel. Positive = up, negative = down.

  {"type": "sleep", "seconds": float}
      Wait without sending any input. Useful when a dialog needs time to open.

  {"type": "terminate"}
      Stop the trajectory. Emit this when CURRENT_STATE already matches
      GOAL_STATE, or when you are confident no further actions will help.

  {"type": "menu_navigate", "path": ["View", "Panels", "Python console"]}
      Compound macro: navigate a nested FreeCAD menu in one shot. The
      executor knows the click+hover sequence required by Qt to open
      submenus reliably. Supported paths (use these exact strings):
        ["View", "Panels", "Python console"]
        ["View", "Panels", "Report view"]
        ["View", "Panels", "Selection view"]
        ["View", "Panels", "Combo View"]
        ["Part", "Primitives", "Box"]           (requires Part workbench)
      Prefer this over manually chaining click actions for menus —
      single clicks between turns can let the dropdown auto-close.

  {"type": "switch_workbench", "name": "Part"}
      Compound macro: open the workbench-selector dropdown and click
      the named workbench. Supported names: Part, Start, Sketcher,
      Part Design, Draft, Mesh Design.

  {"type": "focus_viewport"}
      Compound macro: click in the 3D viewport area to transfer
      keyboard focus to it. ALWAYS issue this immediately before
      pressing view-manipulation keys ("0", "1", "v", "f", "Home")
      if any docked panel (Python console, Combo View, etc.) might
      currently hold focus — otherwise the keystrokes are typed into
      that panel instead of acting on the 3D view.
"""


@dataclass
class ExecutionResult:
    ok: bool
    error: str | None = None
    post_action_sleep: float = 0.5  # default settle time after the action


class ActionExecutor:
    """Validates and executes one parsed action via pyautogui.

    pyautogui is imported lazily so DISPLAY can be set first by the caller.
    """

    def __init__(self):
        import pyautogui  # noqa: PLC0415
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.05
        self._pg = pyautogui

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

    # --- individual action handlers ----------------------------------------

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
        seconds = max(0.0, min(seconds, 10.0))  # clamp 0..10s
        time.sleep(seconds)
        return ExecutionResult(True, post_action_sleep=0.0)

    def _do_terminate(self, a: dict) -> ExecutionResult:
        return ExecutionResult(True, post_action_sleep=0.0)

    def _do_menu_navigate(self, a: dict) -> ExecutionResult:
        raw_path = a.get("path")
        if not isinstance(raw_path, list) or not all(isinstance(p, str) for p in raw_path):
            return ExecutionResult(False, "'menu_navigate' requires 'path': [str, ...]")
        key = tuple(raw_path)
        sequence = MENU_PATHS.get(key)
        if sequence is None:
            return ExecutionResult(
                False,
                f"unknown menu path {raw_path!r}; supported: {sorted(MENU_PATHS.keys())}",
            )
        for kind, x, y, delay in sequence:
            if kind == "click":
                self._pg.moveTo(x, y, duration=0.15)
                self._pg.click()
            elif kind == "hover":
                self._pg.moveTo(x, y, duration=0.15)
            time.sleep(delay)
        return ExecutionResult(True, post_action_sleep=0.6)

    def _do_switch_workbench(self, a: dict) -> ExecutionResult:
        name = a.get("name")
        if not isinstance(name, str) or not name:
            return ExecutionResult(False, "'switch_workbench' requires string 'name'")
        coords = WORKBENCH_COORDS.get(name)
        if coords is None:
            return ExecutionResult(
                False,
                f"unknown workbench {name!r}; supported: {sorted(WORKBENCH_COORDS.keys())}",
            )
        btn_x, btn_y, item_x, item_y = coords
        self._pg.moveTo(btn_x, btn_y, duration=0.15)
        self._pg.click()
        time.sleep(1.0)
        self._pg.moveTo(item_x, item_y, duration=0.15)
        self._pg.click()
        # Workbench switch reflows the toolbar — give the UI ~2s to settle.
        return ExecutionResult(True, post_action_sleep=2.0)

    def _do_focus_viewport(self, a: dict) -> ExecutionResult:
        x, y = VIEWPORT_FOCUS_XY
        self._pg.moveTo(x, y, duration=0.15)
        self._pg.click()
        return ExecutionResult(True, post_action_sleep=0.5)

    @staticmethod
    def _xy(a: dict) -> tuple[int, int]:
        x, y = a.get("x"), a.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise ValueError("integer 'x' and 'y' required")
        return int(x), int(y)
