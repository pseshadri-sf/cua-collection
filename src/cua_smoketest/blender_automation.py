from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BlenderLaunch:
    pid: int
    window_id: str | None
    window_title: str | None


class BlenderAutomation:
    """Process-mode Blender driver: subprocess launch + xdotool keystrokes.

    For each asset we restart Blender with the file as a CLI arg, then
    send `Home` (View All) to frame the scene before screenshotting.
    Mirrors FreeCADAutomation in shape so the Smoketest orchestrator can
    drive either with the same lifecycle.
    """

    def __init__(self, display: str, logs_dir: Path,
                 blender_binary: str | None = None,
                 window_timeout: float = 60.0):
        self.display = display
        self.logs_dir = logs_dir
        self.blender_binary = blender_binary or shutil.which("blender")
        self.window_timeout = window_timeout
        self._proc: subprocess.Popen | None = None

    # --- lifecycle ---------------------------------------------------------

    def launch(self, asset: Path | None = None) -> BlenderLaunch:
        if not self.blender_binary:
            raise RuntimeError("Blender binary not found")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log = open(self.logs_dir / "blender.log", "ab")
        cmd: list[str] = [self.blender_binary, "--factory-startup"]
        if asset is not None:
            cmd.append(str(asset))
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        # Force Mesa software rendering (no GPU on this box).
        env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
        env.setdefault("LANG", "C.UTF-8")
        self._proc = subprocess.Popen(
            cmd, stdout=log, stderr=log, env=env, start_new_session=True,
        )
        window_id, title = self._wait_for_window(["Blender"], self.window_timeout)
        return BlenderLaunch(pid=self._proc.pid, window_id=window_id,
                             window_title=title)

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
        """Frame all geometry in the 3D viewport.

        Blender's default keymap binds `Home` to `view3d.view_all`. The
        key must reach the 3D viewport area, which means the cursor must
        be hovering over it (Blender uses hover-focus). We move the
        cursor to screen-centre via xdotool first.
        """
        if window_id:
            self._activate_window(window_id)
            time.sleep(0.3)
        # Move pointer into the central 3D viewport area, then send Home.
        self._move_pointer(960, 540)
        time.sleep(0.2)
        self._key("Home")
        time.sleep(0.4)

    def dismiss_splash(self) -> None:
        """Blender shows a splash overlay on launch — Escape dismisses it."""
        time.sleep(0.5)
        self._key("Escape")
        time.sleep(0.3)

    # --- internals ---------------------------------------------------------

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

    def _wait_for_window(self, name_substrings: list[str],
                         timeout: float) -> tuple[str | None, str | None]:
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
            return True
        env = {**os.environ, "DISPLAY": self.display}
        res = subprocess.run(
            [xwininfo, "-id", wid], capture_output=True, text=True,
            env=env, timeout=5,
        )
        return res.returncode == 0
