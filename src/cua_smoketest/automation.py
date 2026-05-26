from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FreeCADLaunch:
    pid: int
    window_id: str | None
    window_title: str | None


class FreeCADAutomation:
    """Drives the FreeCAD GUI: launch, wait-for-window, fit-view, quit.

    Window discovery uses wmctrl; key/mouse events via xdotool.
    pyautogui is used opportunistically for keystrokes (it also needs $DISPLAY).
    """

    def __init__(self, display: str, logs_dir: Path,
                 freecad_binary: str | None = None,
                 window_timeout: float = 90.0):
        self.display = display
        self.logs_dir = logs_dir
        self.freecad_binary = freecad_binary or shutil.which("freecad") or shutil.which("FreeCAD")
        self.window_timeout = window_timeout
        self._proc: subprocess.Popen | None = None

    def launch(self, asset: Path | None = None) -> FreeCADLaunch:
        if not self.freecad_binary:
            raise RuntimeError("FreeCAD binary not found")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log = open(self.logs_dir / "freecad.log", "ab")
        cmd: list[str] = [self.freecad_binary]
        if asset is not None:
            cmd.append(str(asset))
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        env.setdefault("QT_QPA_PLATFORM", "xcb")
        # Disable FreeCAD splash and addon-manager network calls where possible.
        env.setdefault("LANG", "C.UTF-8")
        self._proc = subprocess.Popen(
            cmd, stdout=log, stderr=log, env=env, start_new_session=True,
        )
        window_id, title = self._wait_for_window(["FreeCAD"], self.window_timeout)
        return FreeCADLaunch(pid=self._proc.pid, window_id=window_id, window_title=title)

    def fit_view(self, window_id: str | None = None) -> None:
        # Qt ignores synthetic SendEvent keys (xdotool --window), so we activate
        # the window first and then deliver real keypresses to the focused window.
        if window_id:
            self._activate_window(window_id)
            time.sleep(0.4)
        # 0 = Std_ViewIsometric; V,F = Std_ViewFitAll (default FreeCAD bindings).
        self._key("0")
        time.sleep(0.4)
        self._key("v")
        time.sleep(0.15)
        self._key("f")
        time.sleep(0.6)

    def _activate_window(self, window_id: str) -> None:
        xdotool = shutil.which("xdotool")
        if not xdotool:
            return
        env = {**os.environ, "DISPLAY": self.display}
        subprocess.run([xdotool, "windowactivate", "--sync", window_id],
                       capture_output=True, env=env, timeout=5)

    def quit(self, timeout: float = 5.0) -> None:
        if not self._proc:
            return
        if self._proc.poll() is not None:
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

    def _wait_for_window(self, name_substrings: list[str],
                         timeout: float) -> tuple[str | None, str | None]:
        wmctrl = shutil.which("wmctrl")
        if not wmctrl:
            time.sleep(min(timeout, 8.0))
            return None, None
        deadline = time.time() + timeout
        env = {**os.environ, "DISPLAY": self.display}
        while time.time() < deadline:
            res = subprocess.run(
                [wmctrl, "-l"], capture_output=True, text=True, env=env, timeout=5,
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.split(None, 3)
                    if len(parts) < 4:
                        continue
                    wid, _desktop, _host, title = parts
                    if any(s.lower() in title.lower() for s in name_substrings):
                        return wid, title
            time.sleep(1.0)
        return None, None

    def _key(self, keysym: str) -> None:
        xdotool = shutil.which("xdotool")
        if not xdotool:
            return
        env = {**os.environ, "DISPLAY": self.display}
        subprocess.run([xdotool, "key", "--clearmodifiers", keysym],
                       capture_output=True, env=env, timeout=5)
