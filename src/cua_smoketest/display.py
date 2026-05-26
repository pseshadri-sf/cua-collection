from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DisplaySession:
    display: str
    xvfb_pid: int | None
    openbox_pid: int | None
    reused: bool


class DisplayManager:
    """Provisions an X display via Xvfb + openbox if none exists.

    Reuses an existing $DISPLAY when xdpyinfo confirms it is alive.
    """

    def __init__(self, logs_dir: Path, display: str = ":99",
                 geometry: str = "1920x1080x24"):
        self.logs_dir = logs_dir
        self.display = display
        self.geometry = geometry
        self._xvfb: subprocess.Popen | None = None
        self._openbox: subprocess.Popen | None = None

    def acquire(self) -> DisplaySession:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        existing = os.environ.get("DISPLAY", "").strip()
        if existing and self._display_alive(existing):
            return DisplaySession(
                display=existing, xvfb_pid=None, openbox_pid=None, reused=True
            )

        self._start_xvfb()
        if not self._wait_for_display(self.display, timeout=10.0):
            raise RuntimeError(f"Xvfb on {self.display} never came up")
        self._start_openbox()
        os.environ["DISPLAY"] = self.display
        return DisplaySession(
            display=self.display,
            xvfb_pid=self._xvfb.pid if self._xvfb else None,
            openbox_pid=self._openbox.pid if self._openbox else None,
            reused=False,
        )

    def release(self) -> None:
        for proc in (self._openbox, self._xvfb):
            if proc and proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, ProcessLookupError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass

    def _start_xvfb(self) -> None:
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            raise RuntimeError("Xvfb is not installed")
        log = open(self.logs_dir / "xvfb.log", "ab")
        w, h, d = self.geometry.split("x")
        self._xvfb = subprocess.Popen(
            [xvfb, self.display, "-screen", "0", f"{w}x{h}x{d}", "-nolisten", "tcp"],
            stdout=log, stderr=log, start_new_session=True,
        )

    def _start_openbox(self) -> None:
        openbox = shutil.which("openbox")
        if not openbox:
            return  # acceptable: bare X is enough for FreeCAD
        log = open(self.logs_dir / "openbox.log", "ab")
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        self._openbox = subprocess.Popen(
            [openbox], stdout=log, stderr=log, env=env, start_new_session=True,
        )
        time.sleep(0.5)

    @staticmethod
    def _display_alive(display: str) -> bool:
        xdpyinfo = shutil.which("xdpyinfo")
        if not xdpyinfo:
            return False
        result = subprocess.run(
            [xdpyinfo, "-display", display],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0

    def _wait_for_display(self, display: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._display_alive(display):
                return True
            time.sleep(0.25)
        return False
