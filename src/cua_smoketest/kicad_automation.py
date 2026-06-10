"""Process-mode KiCad (pcbnew) driver: subprocess launch + xdotool keystrokes.

Mirrors FreeCADAutomation in shape so the trajectory runner can drive pcbnew
with the same lifecycle (launch / wait-for-window / fit-view / quit). KiCad's
PCB editor (`pcbnew`) is a separate executable launched directly with a board
file, exactly like FreeCAD/Blender.

KiCad-specific deltas vs FreeCADAutomation (see SPEC-kicad-pipeline.md §2):
  - Cairo (Fallback) GAL canvas + Mesa software GL — KiCad's OpenGL canvas
    NULL-derefs under Xvfb/llvmpipe (GitLab #11751). Forced via env +
    a seeded, isolated KiCad config home.
  - KiCad is wxWidgets, NOT Qt — so there is no QT_QPA_PLATFORM and no
    Qt-SendEvent quirk to work around (verified separately in M0).
  - pcbnew always launches WITH a board (the blank seed board); GetBoard()
    needs an open document, unlike FreeCAD's empty-doc start.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class KiCadLaunch:
    pid: int
    window_id: str | None
    window_title: str | None


# pcbnew window titles across KiCad 7-10 contain one of these substrings.
_WINDOW_SUBSTRINGS = ["PCB Editor", "pcbnew", "KiCad"]


class KiCadAutomation:
    """Drives the pcbnew GUI: launch, wait-for-window, fit-view, quit.

    Window discovery uses wmctrl; key/mouse events via xdotool. Mirrors
    FreeCADAutomation so the Smoketest/trajectory orchestrator can drive it
    with the same calls.
    """

    def __init__(self, display: str, logs_dir: Path,
                 pcbnew_binary: str | None = None,
                 window_timeout: float = 90.0):
        self.display = display
        self.logs_dir = Path(logs_dir)
        self.pcbnew_binary = pcbnew_binary or shutil.which("pcbnew")
        self.window_timeout = window_timeout
        # Isolated KiCad config home so we can force the Cairo canvas + suppress
        # first-run dialogs without clobbering any real user config on the box.
        self.config_home = self.logs_dir / "kicad_config"
        self._proc: subprocess.Popen | None = None

    # --- lifecycle ---------------------------------------------------------

    def launch(self, board: Path) -> KiCadLaunch:
        if not self.pcbnew_binary:
            raise RuntimeError("pcbnew binary not found")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._seed_config()
        log = open(self.logs_dir / "pcbnew.log", "ab")
        cmd: list[str] = [self.pcbnew_binary, str(board)]
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        # Force the Fallback (Cairo) canvas + Mesa software rendering: KiCad's
        # OpenGL GAL canvas crashes under Xvfb/llvmpipe (GitLab #11751).
        env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
        env.setdefault("GALLIUM_DRIVER", "llvmpipe")
        env.setdefault("LANG", "C.UTF-8")
        # Point KiCad at our isolated, pre-seeded config (Cairo canvas, no
        # first-run wizard). KiCad reads a version-suffixed var first, then the
        # generic one — set both so we are version-agnostic across 7-10.
        env["KICAD_CONFIG_HOME"] = str(self.config_home)
        for ver in ("7", "8", "9", "10"):
            env.setdefault(f"KICAD{ver}_CONFIG_HOME", str(self.config_home))
        self._proc = subprocess.Popen(
            cmd, stdout=log, stderr=log, env=env, start_new_session=True,
        )
        window_id, title = self._wait_for_window(_WINDOW_SUBSTRINGS, self.window_timeout)
        # pcbnew launches un-maximized (~1280px); maximize so the canvas + panel
        # coordinates the action executor uses are stable at 1920x1080.
        if window_id:
            self._maximize_window(window_id)
        return KiCadLaunch(pid=self._proc.pid, window_id=window_id, window_title=title)

    def _maximize_window(self, window_id: str) -> None:
        wmctrl = shutil.which("wmctrl")
        env = {**os.environ, "DISPLAY": self.display}
        try:
            if wmctrl:
                subprocess.run([wmctrl, "-i", "-r", window_id, "-b",
                                "add,maximized_vert,maximized_horz"],
                               capture_output=True, env=env, timeout=5)
            else:
                xdotool = shutil.which("xdotool")
                if xdotool:
                    subprocess.run([xdotool, "windowsize", window_id, "1920", "1080"],
                                   capture_output=True, env=env, timeout=5)
            time.sleep(1.0)
        except subprocess.SubprocessError:
            pass

    def quit(self, timeout: float = 5.0) -> None:
        if not self._proc or self._proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

    # --- view manipulation -------------------------------------------------

    def fit_view(self, window_id: str | None = None) -> None:
        """Zoom-to-fit the PCB canvas. KiCad binds `Home` to "Zoom to Fit".

        The key must reach the canvas, which uses hover-focus, so we move the
        pointer into the canvas centre first (mirrors the FreeCAD/Blender
        fit-view pattern). Primary framing is done in-console via pcbnew's API
        in the action executor; this is the belt-and-suspenders real-input path.
        """
        if window_id:
            self._activate_window(window_id)
            time.sleep(0.3)
        self._move_pointer(960, 540)
        time.sleep(0.2)
        self._key("Home")
        time.sleep(0.4)

    def dismiss_dialogs(self) -> None:
        """Dismiss the KiCad first-run "KiCad Setup" wizard if present.

        Validated against KiCad 10 under Xvfb (M0): the wizard appears on EVERY
        launch (Cancel→Yes uses defaults but doesn't mark setup complete), and
        KiCad is wxWidgets so synthetic `xdotool key --window` events are
        dropped — only real mouse clicks register. So we locate the wizard
        window, click its Cancel button (bottom-right of the dialog), then click
        Yes on the "Are you sure?" Confirmation. Idempotent: if no wizard is
        present this is a no-op (nothing is clicked). Mirrors the
        dismiss_splash role for the other apps.
        """
        wiz = self._find_window("KiCad Setup")
        if not wiz:
            time.sleep(1.0)
            wiz = self._find_window("KiCad Setup")  # brief retry — may still be opening
        if not wiz:
            return
        g = self._window_geometry(wiz)
        if not g:
            return
        # Cancel button: bottom-right of the wizard dialog (M0-measured offsets).
        self._click(g["X"] + g["WIDTH"] - 55, g["Y"] + g["HEIGHT"] - 48)
        time.sleep(1.5)
        conf = self._find_window("Confirmation")
        if conf:
            gc = self._window_geometry(conf)
            if gc:
                # Yes button: bottom-left quadrant of the confirmation.
                self._click(gc["X"] + int(gc["WIDTH"] * 0.25), gc["Y"] + gc["HEIGHT"] - 40)
                time.sleep(1.5)

    # --- config seeding ----------------------------------------------------

    def _seed_config(self) -> None:
        """Seed an isolated KiCad config that forces the Fallback (Cairo) canvas
        and suppresses the first-run wizard, so launches under Xvfb don't crash
        on the OpenGL canvas or block on a modal.

        Best-effort: the exact pcbnew.json key for canvas selection is confirmed
        in the M0 infra spike (SPEC §2.3); we write the candidate keys here and
        never raise. Writing into our own KICAD_CONFIG_HOME means we never touch
        a real user config.
        """
        try:
            self.config_home.mkdir(parents=True, exist_ok=True)
            # kicad_common.json: skip the first-run "configure paths" wizard.
            common = self.config_home / "kicad_common.json"
            if not common.exists():
                common.write_text(json.dumps({
                    "do_not_show_again": {
                        "zone_fill_warning": True,
                        "scaled_3d_models_warning": True,
                        "data_collection_prompt": True,
                        "update_check_prompt": True,
                    },
                    "system": {"first_run_shown": True},
                }, indent=2))
            # pcbnew.json: force the software (Cairo / "fallback") canvas.
            # M0 confirms the precise key for KiCad 10; both candidate spellings
            # are written so whichever the build honours takes effect.
            pcbnew = self.config_home / "pcbnew.json"
            if not pcbnew.exists():
                pcbnew.write_text(json.dumps({
                    "canvas": {"use_accelerated_graphics": False},
                    "graphics": {"canvas_type": 1},  # 1 == Cairo/fallback GAL
                }, indent=2))
        except OSError:
            pass

    # --- internals (shared shape with FreeCADAutomation) -------------------

    def _activate_window(self, window_id: str) -> None:
        xdotool = shutil.which("xdotool")
        if not xdotool:
            return
        env = {**os.environ, "DISPLAY": self.display}
        subprocess.run([xdotool, "windowactivate", "--sync", window_id],
                       capture_output=True, env=env, timeout=5)

    def _key(self, keysym: str) -> None:
        xdotool = shutil.which("xdotool")
        if not xdotool:
            return
        env = {**os.environ, "DISPLAY": self.display}
        subprocess.run([xdotool, "key", "--clearmodifiers", keysym],
                       capture_output=True, env=env, timeout=5)

    def _move_pointer(self, x: int, y: int) -> None:
        xdotool = shutil.which("xdotool")
        if not xdotool:
            return
        env = {**os.environ, "DISPLAY": self.display}
        subprocess.run([xdotool, "mousemove", str(x), str(y)],
                       capture_output=True, env=env, timeout=5)

    def _click(self, x: int, y: int) -> None:
        """Real mouse click (registers with wxWidgets, unlike synthetic keys)."""
        xdotool = shutil.which("xdotool")
        if not xdotool:
            return
        env = {**os.environ, "DISPLAY": self.display}
        subprocess.run([xdotool, "mousemove", str(x), str(y), "click", "1"],
                       capture_output=True, env=env, timeout=5)

    def _find_window(self, name: str) -> str | None:
        xdotool = shutil.which("xdotool")
        if not xdotool:
            return None
        env = {**os.environ, "DISPLAY": self.display}
        res = subprocess.run([xdotool, "search", "--name", name],
                             capture_output=True, text=True, env=env, timeout=5)
        ids = res.stdout.split()
        return ids[-1] if ids else None

    def _window_geometry(self, window_id: str) -> dict | None:
        xdotool = shutil.which("xdotool")
        if not xdotool:
            return None
        env = {**os.environ, "DISPLAY": self.display}
        res = subprocess.run([xdotool, "getwindowgeometry", "--shell", window_id],
                             capture_output=True, text=True, env=env, timeout=5)
        g: dict = {}
        for line in res.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                g[k] = int(v) if v.lstrip("-").isdigit() else v
        return g if {"X", "Y", "WIDTH", "HEIGHT"} <= g.keys() else None

    def _wait_for_window(self, name_substrings: list[str],
                         timeout: float) -> tuple[str | None, str | None]:
        """Wait for the pcbnew main window owned by `self._proc`.

        Filters wmctrl results to the launched PID via xdotool --pid so we don't
        latch onto a transient splash/dialog window. Same logic as
        FreeCADAutomation._wait_for_window.
        """
        wmctrl = shutil.which("wmctrl")
        xdotool = shutil.which("xdotool")
        if not wmctrl:
            time.sleep(min(timeout, 8.0))
            return None, None
        deadline = time.time() + timeout
        env = {**os.environ, "DISPLAY": self.display}
        target_pid = self._proc.pid if self._proc else None
        last_seen: str | None = None
        while time.time() < deadline:
            owned_hex: set[str] = set()
            if xdotool and target_pid is not None:
                xs = subprocess.run(
                    [xdotool, "search", "--pid", str(target_pid)],
                    capture_output=True, text=True, env=env, timeout=5,
                )
                if xs.returncode == 0:
                    owned_hex = {
                        f"0x{int(line):08x}"
                        for line in xs.stdout.split() if line.isdigit()
                    }
            res = subprocess.run(
                [wmctrl, "-l"], capture_output=True, text=True, env=env, timeout=5,
            )
            candidates: list[tuple[str, str]] = []
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.split(None, 3)
                    if len(parts) < 4:
                        continue
                    wid, _desktop, _host, title = parts
                    if target_pid is not None and owned_hex and wid.lower() not in owned_hex:
                        continue
                    if any(s.lower() in title.lower() for s in name_substrings):
                        candidates.append((wid, title))
            if candidates:
                wid, title = candidates[0]
                if wid == last_seen:
                    time.sleep(2.0)
                    if self._window_alive(wid):
                        return wid, title
                    last_seen = None
                    continue
                last_seen = wid
            else:
                last_seen = None
            time.sleep(0.5)
        return None, None

    def _window_alive(self, wid: str) -> bool:
        xwininfo = shutil.which("xwininfo")
        if not xwininfo:
            return True  # best-effort: assume alive if we can't check
        env = {**os.environ, "DISPLAY": self.display}
        res = subprocess.run(
            [xwininfo, "-id", wid], capture_output=True, text=True,
            env=env, timeout=5,
        )
        return res.returncode == 0
