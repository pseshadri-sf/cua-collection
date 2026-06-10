"""Action space for the KiCad (pcbnew) trajectory harness.

Mirrors the FreeCAD ActionExecutor (NOT Blender): KiCad's Scripting Console is a
docked, *toggling* menu panel just like FreeCAD's Python console, so the same
robustness machinery applies and is inherited here:
  - open-once `_console_opened` latch (the menu item toggles the panel),
  - `open_scripting_console(verify=True)` marker-file verification,
  - `pcbnew_eval` runs code via a FILE + a short `exec(open(...).read())` line
    (typing long footprint/track lines char-by-char into a console is unreliable),
  - an in-console frame script (pcbnew.Refresh) run focus-independently after
    every build, plus a belt-and-suspenders real-input zoom-to-fit.

The primary verb is `pcbnew_eval`. Structured PCB actions (place_footprint,
route_track, ...) are validated, typed wrappers that render to a pcbnew snippet
and route through the same `pcbnew_eval` path. Units are millimetres via
pcbnew.FromMM, matching FreeCAD's mm model.

KiCad 10 on Xvfb 1920x1080, Fallback (Cairo) GAL canvas. Coordinates marked
"M0" are confirmed/adjusted during the infra spike (SPEC-kicad-pipeline.md §3).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any


VALID_TYPES = {
    "move_to", "click", "double_click", "right_click",
    "type", "key", "hotkey", "scroll", "sleep", "terminate",
    "menu_navigate", "open_scripting_console", "focus_canvas",
    "pcbnew_eval", "frame_view",
    # --- v1 structured PCB actions (translate to pcbnew_eval) ---
    "place_footprint", "move_footprint", "route_track", "add_via",
    "add_zone", "set_board_outline", "add_net", "assign_pad_net",
}


# Where to click to give the PCB canvas keyboard focus (M0-measured).
CANVAS_FOCUS_XY: tuple[int, int] = (860, 520)
# Scripting Console input row after Tools>Scripting Console docks it (M0).
PCBNEW_CONSOLE_INPUT_XY: tuple[int, int] = (700, 1000)


# Pre-measured click/hover sequence to open the Scripting Console via the menu
# (Tools menu, "Scripting Console" item). Coords are M0-measured on KiCad 10
# 1920x1080. The menu item TOGGLES the panel — open exactly once (see
# `_console_opened`). Mirrors FreeCAD's MENU_PATHS.
MENU_PATHS: dict[tuple[str, ...], list[tuple[str, int, int, float]]] = {
    ("Tools", "Scripting Console"): [
        ("click", 330, 38, 0.8),    # Tools menu in the menubar (M0)
        ("click", 380, 300, 0.8),   # "Scripting Console" item (M0)
    ],
}


# Authoritative human-readable spec embedded in the system prompt.
ACTION_SPACE_SPEC_KICAD = """\
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
      Press a single key. Useful: enter, escape, tab, Home (zoom-to-fit
      the canvas), Delete.

  {"type": "hotkey", "keys": ["ctrl", "a"]}
      Chord: press keys together.

  {"type": "scroll", "clicks": int}
      Scroll wheel (zoom on the PCB canvas). Positive = in, negative = out.

  {"type": "sleep", "seconds": float}
      Wait without sending input. The Cairo canvas is slow; sleep ~1.5s after a
      build before relying on a screenshot.

  {"type": "terminate"}
      Stop the trajectory. Emit when CURRENT_STATE already matches GOAL_STATE.

  {"type": "menu_navigate", "path": ["Tools", "Scripting Console"]}
      Compound macro: open the Tools > Scripting Console panel. The console is
      where you run pcbnew Python to build the board.

  {"type": "pcbnew_eval", "code": "import pcbnew; b=pcbnew.GetBoard(); ..."}
      PREFERRED for all board construction. Compound macro that opens the
      Scripting Console once, focuses its input, runs `code` (via a file so
      long lines are reliable), then refreshes the canvas. `b=pcbnew.GetBoard()`
      returns the LIVE open board — mutate it (Add footprints/tracks/zones) and
      the change appears on the canvas. ALWAYS end your code with
      `pcbnew.Refresh()`. Units are nanometres internally; ALWAYS wrap mm with
      pcbnew.FromMM(...) and positions with pcbnew.VECTOR2I(...).
      Example:
        import pcbnew; b=pcbnew.GetBoard(); fp=pcbnew.FootprintLoad('Resistor_SMD.pretty','R_0805_2012Metric'); fp.SetReference('R1'); fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(20),pcbnew.FromMM(15))); b.Add(fp); pcbnew.Refresh()

  {"type": "frame_view"}
      Compound macro: focus the canvas + press Home (Zoom to Fit). Use after a
      build if the new geometry is off-screen.
"""


# Run in the pcbnew Scripting Console after every pcbnew_eval to guarantee the
# board redraws. KiCad's scripting API has no clean "zoom to fit" hook, so view
# framing is handled by the belt-and-suspenders real-input Home key in
# _do_pcbnew_eval; this script just forces a model->canvas refresh. Robust to
# any binding/version differences. Mirrors FreeCAD's _FRAME_SCRIPT.
_FRAME_SCRIPT_KICAD = '''\
try:
    import pcbnew
    pcbnew.Refresh()
except Exception:
    pass
'''


@dataclass
class ExecutionResult:
    ok: bool
    error: str | None = None
    post_action_sleep: float = 0.5


class KiCadActionExecutor:
    """Validates and executes one parsed action via pyautogui + xdotool.

    pyautogui is imported lazily so DISPLAY can be set first by the caller.
    Mirrors the FreeCAD ActionExecutor; the console-open verification, open-once
    latch, and file+exec eval path are the reason KiCad mirrors FreeCAD (not
    Blender) — the Scripting Console is the same docked, toggling-menu panel.
    """

    def __init__(self, state_probe_path: "str | None" = None):
        import pyautogui  # noqa: PLC0415
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.05
        self._pg = pyautogui
        self._state_probe_path = state_probe_path
        # Frame script: force a canvas refresh focus-independently after every
        # build. Written once to a fixed path.
        self._frame_path = "/tmp/cua_kicad_frame.py"
        try:
            with open(self._frame_path, "w") as fh:
                fh.write(_FRAME_SCRIPT_KICAD)
        except OSError:
            self._frame_path = None
        # Per-worker file the build code is written to and exec'd from — typing a
        # short exec(open(...)) line is far more reliable than typing complex
        # multi-statement pcbnew code char-by-char.
        self._eval_path = "/tmp/cua_kicad_eval_%d.py" % os.getpid()
        # The Tools>Scripting Console menu item TOGGLES the panel. Calling it
        # every step would flip it open/closed, so on alternate steps the
        # console would be CLOSED and typed code would go nowhere. Open once.
        self._console_opened = False

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

    # --- KiCad-specific compound actions -----------------------------------

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
        if key == ("Tools", "Scripting Console"):
            self._console_opened = True
        return ExecutionResult(True, post_action_sleep=0.8)

    def _do_open_scripting_console(self, a: dict) -> ExecutionResult:
        self.open_scripting_console(verify=False)
        return ExecutionResult(True, post_action_sleep=0.8)

    def _do_focus_canvas(self, a: dict) -> ExecutionResult:
        x, y = CANVAS_FOCUS_XY
        self._pg.moveTo(x, y, duration=0.15)
        self._pg.click()
        return ExecutionResult(True, post_action_sleep=0.5)

    def _nav_open_console(self) -> None:
        seq = MENU_PATHS.get(("Tools", "Scripting Console"))
        if seq is None:
            return
        for kind, x, y, delay in seq:
            if kind == "click":
                self._pg.moveTo(x, y, duration=0.15); self._pg.click()
            elif kind == "hover":
                self._pg.moveTo(x, y, duration=0.15)
            time.sleep(delay)
        time.sleep(0.5)

    def open_scripting_console(self, verify: bool = False) -> None:
        """Open the pcbnew Scripting Console once. With verify=True, confirm it
        is open + focused by running a command that writes a marker FILE and
        checking the file appeared; retry the menu-open if not. Removes the
        dependence on a single flaky menu click (the #1 cause of empty builds on
        the FreeCAD path, which this mirrors)."""
        cx, cy = PCBNEW_CONSOLE_INPUT_XY
        if not verify:
            self._nav_open_console()
            self._console_opened = True
            return
        marker = "/tmp/cua_kicad_console_ok_%d" % os.getpid()
        for _attempt in range(4):
            self._nav_open_console()
            try:
                if os.path.exists(marker):
                    os.remove(marker)
            except OSError:
                pass
            self._pg.moveTo(cx, cy, duration=0.15); self._pg.click()
            time.sleep(0.3)
            self._pg.typewrite("open(r'%s','w').close()" % marker, interval=0.01)
            time.sleep(0.15)
            self._pg.press("enter")
            time.sleep(0.6)
            if os.path.exists(marker):
                try:
                    os.remove(marker)
                except OSError:
                    pass
                self._console_opened = True
                return
        # give up after retries; mark opened so steps still attempt to type
        self._console_opened = True

    def _do_pcbnew_eval(self, a: dict) -> ExecutionResult:
        """Atomic: open console (idempotent) + focus input + run code (via
        file) + refresh canvas + (optional) state probe + belt-and-suspenders
        zoom-to-fit. Mirrors FreeCAD _do_python_eval."""
        code = a.get("code")
        if not isinstance(code, str) or not code.strip():
            return ExecutionResult(False, "'pcbnew_eval' requires non-empty string 'code'")
        # Write the build code to a file and run it via a SHORT typed line, then
        # run the frame script (canvas refresh). Typing `exec(open(...))` is
        # robust where typing complex multi-statement pcbnew code is not.
        try:
            with open(self._eval_path, "w") as fh:
                fh.write(code)
        except OSError:
            return ExecutionResult(False, "could not write eval file")
        typed = "exec(open(r'%s').read())" % self._eval_path
        if self._frame_path:
            typed += ";exec(open(r'%s').read())" % self._frame_path
        cx, cy = PCBNEW_CONSOLE_INPUT_XY
        # 1. Open the console exactly ONCE and keep it open (the menu toggles).
        if not self._console_opened:
            self.open_scripting_console()
        # 2. Focus the console input (click it; harmless if already focused).
        self._pg.moveTo(cx, cy, duration=0.15)
        self._pg.click()
        time.sleep(0.3)
        # 3. Type the short exec line + Enter.
        self._pg.typewrite(typed, interval=0.01)
        time.sleep(0.2)
        self._pg.press("enter")
        time.sleep(1.5)  # Cairo software canvas needs time to repaint
        # 4. Optional state probe as a SEPARATE short submission (keeps the
        #    typed line short so it lands reliably).
        if self._state_probe_path:
            self._pg.moveTo(cx, cy, duration=0.15)
            self._pg.click()
            time.sleep(0.2)
            self._pg.typewrite("exec(open(r'%s').read())" % self._state_probe_path,
                               interval=0.01)
            time.sleep(0.15)
            self._pg.press("enter")
            time.sleep(0.5)
        # 5. Belt-and-suspenders: focus canvas + Home (Zoom to Fit), since the
        #    scripting API has no clean zoom hook.
        vx, vy = CANVAS_FOCUS_XY
        self._pg.moveTo(vx, vy, duration=0.15)
        self._pg.click()
        time.sleep(0.3)
        self._pg.press("Home")
        return ExecutionResult(True, post_action_sleep=0.6)

    def _do_frame_view(self, a: dict) -> ExecutionResult:
        """Atomic: focus canvas + Home (Zoom to Fit)."""
        x, y = CANVAS_FOCUS_XY
        self._pg.moveTo(x, y, duration=0.15)
        self._pg.click()
        time.sleep(0.3)
        self._pg.press("Home")
        return ExecutionResult(True, post_action_sleep=0.6)

    # --- v1 structured PCB actions (render to a pcbnew snippet) ------------
    # Each validates typed parameters then routes a generated pcbnew snippet
    # through _do_pcbnew_eval. Layer-name -> id uses board.GetLayerID(name) so
    # the driver process never imports pcbnew. Units via pcbnew.FromMM. Net by
    # name via board.FindNet(name). Mirrors the FreeCAD build_*/cut/fuse design.

    @staticmethod
    def _vec(x_mm: float, y_mm: float) -> str:
        return f"pcbnew.VECTOR2I(pcbnew.FromMM({x_mm}),pcbnew.FromMM({y_mm}))"

    def _do_place_footprint(self, a: dict) -> ExecutionResult:
        lib = a.get("lib"); name = a.get("name"); ref = a.get("ref")
        at = a.get("at") or [0, 0]; rot = a.get("rot_deg", 0); layer = a.get("layer", "F.Cu")
        if not all(isinstance(s, str) and s.strip() for s in (lib, name, ref)):
            return ExecutionResult(False, "'place_footprint' requires 'lib', 'name', 'ref' strings")
        if not (isinstance(at, (list, tuple)) and len(at) == 2):
            return ExecutionResult(False, "'place_footprint' requires 'at': [x_mm, y_mm]")
        flip = (
            f";fp.SetLayerAndFlip(b.GetLayerID({layer!r}))" if layer == "B.Cu" else ""
        )
        code = (
            f"import pcbnew;b=pcbnew.GetBoard();fp=pcbnew.FootprintLoad({lib!r},{name!r});"
            f"fp.SetReference({ref!r});fp.SetPosition({self._vec(at[0], at[1])});"
            f"fp.SetOrientationDegrees({rot});b.Add(fp){flip};pcbnew.Refresh()"
        )
        return self._do_pcbnew_eval({"code": code})

    def _do_move_footprint(self, a: dict) -> ExecutionResult:
        ref = a.get("ref"); at = a.get("at"); rot = a.get("rot_deg")
        if not (isinstance(ref, str) and ref.strip()):
            return ExecutionResult(False, "'move_footprint' requires 'ref' string")
        if not (isinstance(at, (list, tuple)) and len(at) == 2):
            return ExecutionResult(False, "'move_footprint' requires 'at': [x_mm, y_mm]")
        rot_stmt = f";fp.SetOrientationDegrees({rot})" if isinstance(rot, (int, float)) else ""
        code = (
            f"import pcbnew;b=pcbnew.GetBoard();fp=b.FindFootprintByReference({ref!r});"
            f"fp.SetPosition({self._vec(at[0], at[1])}){rot_stmt};pcbnew.Refresh()"
        )
        return self._do_pcbnew_eval({"code": code})

    def _do_route_track(self, a: dict) -> ExecutionResult:
        start = a.get("start"); end = a.get("end"); width = a.get("width_mm", 0.25)
        layer = a.get("layer", "F.Cu"); net = a.get("net")
        for pt, key in ((start, "start"), (end, "end")):
            if not (isinstance(pt, (list, tuple)) and len(pt) == 2):
                return ExecutionResult(False, f"'route_track' requires '{key}': [x_mm, y_mm]")
        if not isinstance(width, (int, float)) or width <= 0:
            return ExecutionResult(False, "'route_track' requires positive 'width_mm'")
        net_stmt = (
            f";_n=b.FindNet({net!r});_t.SetNet(_n) if _n else None" if isinstance(net, str) else ""
        )
        code = (
            f"import pcbnew;b=pcbnew.GetBoard();_t=pcbnew.PCB_TRACK(b);"
            f"_t.SetStart({self._vec(start[0], start[1])});_t.SetEnd({self._vec(end[0], end[1])});"
            f"_t.SetWidth(pcbnew.FromMM({width}));_t.SetLayer(b.GetLayerID({layer!r}))"
            f"{net_stmt};b.Add(_t);pcbnew.Refresh()"
        )
        return self._do_pcbnew_eval({"code": code})

    def _do_add_via(self, a: dict) -> ExecutionResult:
        at = a.get("at"); drill = a.get("drill_mm", 0.4); dia = a.get("diameter_mm", 0.8)
        net = a.get("net")
        if not (isinstance(at, (list, tuple)) and len(at) == 2):
            return ExecutionResult(False, "'add_via' requires 'at': [x_mm, y_mm]")
        net_stmt = (
            f";_n=b.FindNet({net!r});_v.SetNet(_n) if _n else None" if isinstance(net, str) else ""
        )
        code = (
            f"import pcbnew;b=pcbnew.GetBoard();_v=pcbnew.PCB_VIA(b);"
            f"_v.SetPosition({self._vec(at[0], at[1])});_v.SetDrill(pcbnew.FromMM({drill}));"
            f"_v.SetWidth(pcbnew.FromMM({dia}))"
            f"{net_stmt};b.Add(_v);pcbnew.Refresh()"
        )
        return self._do_pcbnew_eval({"code": code})

    def _do_set_board_outline(self, a: dict) -> ExecutionResult:
        """Draw a rectangular (or polygon) board outline on Edge_Cuts."""
        rect = a.get("rect"); polygon = a.get("polygon"); origin = a.get("origin") or [0, 0]
        if isinstance(rect, (list, tuple)) and len(rect) == 2:
            ox, oy = origin[0], origin[1]; w, h = rect
            pts = [(ox, oy), (ox + w, oy), (ox + w, oy + h), (ox, oy + h)]
        elif isinstance(polygon, list) and len(polygon) >= 3 and all(
                isinstance(p, (list, tuple)) and len(p) == 2 for p in polygon):
            pts = [(p[0], p[1]) for p in polygon]
        else:
            return ExecutionResult(False, "'set_board_outline' requires 'rect':[w,h] or 'polygon':[[x,y],...]")
        segs = []
        for i in range(len(pts)):
            s, e = pts[i], pts[(i + 1) % len(pts)]
            segs.append(
                f"_s=pcbnew.PCB_SHAPE(b);_s.SetShape(pcbnew.SHAPE_T_SEGMENT);"
                f"_s.SetStart({self._vec(s[0], s[1])});_s.SetEnd({self._vec(e[0], e[1])});"
                f"_s.SetLayer(b.GetLayerID('Edge_Cuts'));b.Add(_s)"
            )
        code = "import pcbnew;b=pcbnew.GetBoard();" + ";".join(segs) + ";pcbnew.Refresh()"
        return self._do_pcbnew_eval({"code": code})

    def _do_add_net(self, a: dict) -> ExecutionResult:
        name = a.get("name")
        if not (isinstance(name, str) and name.strip()):
            return ExecutionResult(False, "'add_net' requires non-empty 'name'")
        code = (
            f"import pcbnew;b=pcbnew.GetBoard();"
            f"b.Add(pcbnew.NETINFO_ITEM(b,{name!r})) if not b.FindNet({name!r}) else None;"
            f"pcbnew.Refresh()"
        )
        return self._do_pcbnew_eval({"code": code})

    def _do_assign_pad_net(self, a: dict) -> ExecutionResult:
        ref = a.get("ref"); pad = a.get("pad"); net = a.get("net")
        if not (isinstance(ref, str) and ref.strip()):
            return ExecutionResult(False, "'assign_pad_net' requires 'ref' string")
        if not (isinstance(net, str) and net.strip()):
            return ExecutionResult(False, "'assign_pad_net' requires 'net' string")
        pad_sel = f"[p for p in fp.Pads() if p.GetPadName()=={str(pad)!r}]" if pad is not None else "list(fp.Pads())"
        code = (
            f"import pcbnew;b=pcbnew.GetBoard();fp=b.FindFootprintByReference({ref!r});"
            f"_n=b.FindNet({net!r});[p.SetNet(_n) for p in {pad_sel} if _n];pcbnew.Refresh()"
        )
        return self._do_pcbnew_eval({"code": code})

    def _do_add_zone(self, a: dict) -> ExecutionResult:
        """Add a filled copper zone over a rectangular outline on a layer/net."""
        rect = a.get("rect"); origin = a.get("origin") or [0, 0]
        layer = a.get("layer", "F.Cu"); net = a.get("net")
        if not (isinstance(rect, (list, tuple)) and len(rect) == 2):
            return ExecutionResult(False, "'add_zone' requires 'rect': [w_mm, h_mm]")
        ox, oy = origin[0], origin[1]; w, h = rect
        corners = [(ox, oy), (ox + w, oy), (ox + w, oy + h), (ox, oy + h)]
        add_pts = ";".join(
            f"_o.Append({self._vec(c[0], c[1])})" for c in corners
        )
        net_stmt = (
            f"_n=b.FindNet({net!r});_z.SetNet(_n) if _n else None;" if isinstance(net, str) else ""
        )
        code = (
            f"import pcbnew;b=pcbnew.GetBoard();_z=pcbnew.ZONE(b);"
            f"_z.SetLayer(b.GetLayerID({layer!r}));{net_stmt}"
            f"_o=_z.Outline();_o.NewOutline();{add_pts};"
            f"b.Add(_z);pcbnew.GetBoard().BuildConnectivity();"
            f"pcbnew.ZONE_FILLER(b).Fill(b.Zones());pcbnew.Refresh()"
        )
        return self._do_pcbnew_eval({"code": code})

    @staticmethod
    def _xy(a: dict) -> tuple[int, int]:
        x, y = a.get("x"), a.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise ValueError("integer 'x' and 'y' required")
        return int(x), int(y)
