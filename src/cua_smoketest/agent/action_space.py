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
    "python_eval", "frame_view",
    # --- v1 structured actions (Qwen-only path; python_eval remains escape hatch) ---
    "build_box", "build_cylinder", "build_sphere", "build_torus",
    "cut", "fuse", "compound",
}


# Where to click to give the 3D viewport keyboard focus on Xvfb/Qt.
VIEWPORT_FOCUS_XY: tuple[int, int] = (1100, 540)

# Python console input field after View>Panels>Python console docks it.
PYTHON_CONSOLE_INPUT_XY: tuple[int, int] = (700, 990)


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

  {"type": "python_eval", "code": "doc=App.newDocument();import Part;..."}
      Compound macro (FreeCAD equivalent of Blender's python_eval):
      open the Python console if not already open, focus its input
      field, type `code`, press Enter. Replaces the 4-step prelude
      (menu_navigate + click + type + key) with one atomic action —
      saves 3 VLM round-trips per reconstruction attempt and avoids
      the focus-loss failure modes between separate steps.
      The console state is idempotent: if it's already docked, the
      menu_navigate step is a no-op and we just refocus the input.

  {"type": "frame_view"}
      Compound macro: focus viewport, switch to isometric (key "0"),
      then fit-all (keys "v" then "f"). Atomic 4-step view setup
      that the agent otherwise emits as separate steps after every
      reconstruction.
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

    def _do_python_eval(self, a: dict) -> ExecutionResult:
        """Atomic: open console (idempotent) + focus input + type code + Enter.

        Collapses the menu_navigate→click→type→key chain into one action,
        matching Blender's existing python_eval semantics.
        """
        code = a.get("code")
        if not isinstance(code, str) or not code.strip():
            return ExecutionResult(False, "'python_eval' requires non-empty string 'code'")
        # 1. Open the Python console (idempotent — Qt no-ops if already docked).
        seq = MENU_PATHS.get(("View", "Panels", "Python console"))
        if seq is not None:
            for kind, x, y, delay in seq:
                if kind == "click":
                    self._pg.moveTo(x, y, duration=0.15); self._pg.click()
                elif kind == "hover":
                    self._pg.moveTo(x, y, duration=0.15)
                time.sleep(delay)
        # 2. Focus the console's input field.
        cx, cy = PYTHON_CONSOLE_INPUT_XY
        self._pg.moveTo(cx, cy, duration=0.15)
        self._pg.click()
        time.sleep(0.3)
        # 3. Type the code.
        self._pg.typewrite(code, interval=0.01)
        time.sleep(0.2)
        # 4. Execute.
        self._pg.press("enter")
        # Give Mesa software-OpenGL ~1.5s to repaint the viewport with new geometry.
        return ExecutionResult(True, post_action_sleep=1.5)

    def _do_frame_view(self, a: dict) -> ExecutionResult:
        """Atomic: focus viewport + isometric (0) + fit-all (v, f)."""
        x, y = VIEWPORT_FOCUS_XY
        self._pg.moveTo(x, y, duration=0.15)
        self._pg.click()
        time.sleep(0.3)
        self._pg.press("0"); time.sleep(0.2)
        self._pg.press("v"); time.sleep(0.15)
        self._pg.press("f")
        return ExecutionResult(True, post_action_sleep=0.6)

    # --- v1 structured actions ------------------------------------------------
    # Each structured action translates to a deterministic single-line Python
    # snippet and routes through _do_python_eval. The agent supplies typed
    # parameters; we generate the exec-correct Python. Compared to the agent
    # typing the same Python directly via python_eval, this:
    #   - rejects calls missing required dimensions at parse time
    #   - eliminates Python syntax errors
    #   - eliminates name-binding errors (uses obj.Label = name uniformly)
    #
    # NOTE: these are TEMPORARY (v1). Keep python_eval as the escape hatch.

    def _do_build_box(self, a: dict) -> ExecutionResult:
        dims = a.get("dims") or {}
        origin = a.get("origin") or {"x": 0, "y": 0, "z": 0}
        name = a.get("name")
        for k in ("x", "y", "z"):
            if not isinstance(dims.get(k), (int, float)) or dims[k] <= 0:
                return ExecutionResult(False, f"'build_box' requires positive dims.{k} (got {dims.get(k)!r})")
        if not isinstance(name, str) or not name.strip():
            return ExecutionResult(False, "'build_box' requires non-empty 'name'")
        code = (
            f"import Part,FreeCAD as App;"
            f"_doc=App.ActiveDocument or App.newDocument();"
            f"_b=Part.makeBox({dims['x']},{dims['y']},{dims['z']});"
            f"_b.translate(App.Vector({origin['x']},{origin['y']},{origin['z']}));"
            f"_o=_doc.addObject('Part::Feature',{name!r});_o.Shape=_b;_doc.recompute()"
        )
        return self._do_python_eval({"code": code})

    def _do_build_cylinder(self, a: dict) -> ExecutionResult:
        radius = a.get("radius"); height = a.get("height")
        axis = (a.get("axis") or "z").lower()
        origin = a.get("origin") or {"x": 0, "y": 0, "z": 0}
        name = a.get("name")
        if not isinstance(radius, (int, float)) or radius <= 0:
            return ExecutionResult(False, f"'build_cylinder' requires positive 'radius' (got {radius!r})")
        if not isinstance(height, (int, float)) or height <= 0:
            return ExecutionResult(False, f"'build_cylinder' requires positive 'height' (got {height!r})")
        if axis not in ("x", "y", "z"):
            return ExecutionResult(False, f"'axis' must be one of x|y|z (got {axis!r})")
        if not isinstance(name, str) or not name.strip():
            return ExecutionResult(False, "'build_cylinder' requires non-empty 'name'")
        axis_vec = {"x": "App.Vector(1,0,0)", "y": "App.Vector(0,1,0)", "z": "App.Vector(0,0,1)"}[axis]
        code = (
            f"import Part,FreeCAD as App;"
            f"_doc=App.ActiveDocument or App.newDocument();"
            f"_c=Part.makeCylinder({radius},{height},"
            f"App.Vector({origin['x']},{origin['y']},{origin['z']}),{axis_vec});"
            f"_o=_doc.addObject('Part::Feature',{name!r});_o.Shape=_c;_doc.recompute()"
        )
        return self._do_python_eval({"code": code})

    def _do_build_sphere(self, a: dict) -> ExecutionResult:
        radius = a.get("radius")
        origin = a.get("origin") or {"x": 0, "y": 0, "z": 0}
        name = a.get("name")
        if not isinstance(radius, (int, float)) or radius <= 0:
            return ExecutionResult(False, "'build_sphere' requires positive 'radius'")
        if not isinstance(name, str) or not name.strip():
            return ExecutionResult(False, "'build_sphere' requires non-empty 'name'")
        code = (
            f"import Part,FreeCAD as App;"
            f"_doc=App.ActiveDocument or App.newDocument();"
            f"_s=Part.makeSphere({radius});"
            f"_s.translate(App.Vector({origin['x']},{origin['y']},{origin['z']}));"
            f"_o=_doc.addObject('Part::Feature',{name!r});_o.Shape=_s;_doc.recompute()"
        )
        return self._do_python_eval({"code": code})

    def _do_build_torus(self, a: dict) -> ExecutionResult:
        Rmaj = a.get("major_radius"); Rmin = a.get("minor_radius")
        origin = a.get("origin") or {"x": 0, "y": 0, "z": 0}
        name = a.get("name")
        if not isinstance(Rmaj, (int, float)) or Rmaj <= 0:
            return ExecutionResult(False, "'build_torus' requires positive 'major_radius'")
        if not isinstance(Rmin, (int, float)) or Rmin <= 0:
            return ExecutionResult(False, "'build_torus' requires positive 'minor_radius'")
        if not isinstance(name, str) or not name.strip():
            return ExecutionResult(False, "'build_torus' requires non-empty 'name'")
        code = (
            f"import Part,FreeCAD as App;"
            f"_doc=App.ActiveDocument or App.newDocument();"
            f"_t=Part.makeTorus({Rmaj},{Rmin});"
            f"_t.translate(App.Vector({origin['x']},{origin['y']},{origin['z']}));"
            f"_o=_doc.addObject('Part::Feature',{name!r});_o.Shape=_t;_doc.recompute()"
        )
        return self._do_python_eval({"code": code})

    def _do_cut(self, a: dict) -> ExecutionResult:
        from_name = a.get("from"); by_name = a.get("by"); name = a.get("name")
        if not all(isinstance(s, str) and s.strip() for s in (from_name, by_name, name)):
            return ExecutionResult(False, "'cut' requires string 'from', 'by', and 'name'")
        code = (
            f"import FreeCAD as App;_doc=App.ActiveDocument;"
            f"_a=_doc.getObject({from_name!r}).Shape;_b=_doc.getObject({by_name!r}).Shape;"
            f"_r=_a.cut(_b);_o=_doc.addObject('Part::Feature',{name!r});"
            f"_o.Shape=_r;_doc.recompute()"
        )
        return self._do_python_eval({"code": code})

    def _do_fuse(self, a: dict) -> ExecutionResult:
        shapes = a.get("shapes"); name = a.get("name")
        if not isinstance(shapes, list) or len(shapes) < 2 or not all(isinstance(s, str) for s in shapes):
            return ExecutionResult(False, "'fuse' requires list 'shapes' of >=2 names")
        if not isinstance(name, str) or not name.strip():
            return ExecutionResult(False, "'fuse' requires non-empty 'name'")
        names_list = "[" + ",".join(f"_doc.getObject({s!r}).Shape" for s in shapes) + "]"
        code = (
            f"import Part,FreeCAD as App;_doc=App.ActiveDocument;"
            f"_shapes={names_list};_r=_shapes[0]"
            + "".join([f".fuse(_shapes[{i+1}])" for i in range(len(shapes) - 1)]) + ";"
            f"_o=_doc.addObject('Part::Feature',{name!r});_o.Shape=_r;_doc.recompute()"
        )
        return self._do_python_eval({"code": code})

    def _do_compound(self, a: dict) -> ExecutionResult:
        """Like fuse, but without boolean union — keeps the parts distinct."""
        shapes = a.get("shapes"); name = a.get("name")
        if not isinstance(shapes, list) or len(shapes) < 2 or not all(isinstance(s, str) for s in shapes):
            return ExecutionResult(False, "'compound' requires list 'shapes' of >=2 names")
        if not isinstance(name, str) or not name.strip():
            return ExecutionResult(False, "'compound' requires non-empty 'name'")
        names_list = "[" + ",".join(f"_doc.getObject({s!r}).Shape" for s in shapes) + "]"
        code = (
            f"import Part,FreeCAD as App;_doc=App.ActiveDocument;"
            f"_r=Part.makeCompound({names_list});"
            f"_o=_doc.addObject('Part::Feature',{name!r});_o.Shape=_r;_doc.recompute()"
        )
        return self._do_python_eval({"code": code})

    @staticmethod
    def _xy(a: dict) -> tuple[int, int]:
        x, y = a.get("x"), a.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise ValueError("integer 'x' and 'y' required")
        return int(x), int(y)
