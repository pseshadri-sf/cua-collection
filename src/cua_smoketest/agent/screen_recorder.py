"""ffmpeg-based screen recorder for an X11/Xvfb display."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RecorderHandle:
    output_path: Path
    pid: int
    start_monotonic: float


class ScreenRecorder:
    """Records a region of an X display via `ffmpeg -f x11grab`.

    Use:
        rec = ScreenRecorder(display=":99", output=...)
        handle = rec.start()
        # ... do stuff that takes time ...
        rec.stop()
        # video is at handle.output_path

    `start_monotonic` is the time.monotonic() value at which recording
    started; subtract it from later monotonic timestamps to get the
    in-video time of any event.
    """

    def __init__(self, display: str, output: Path,
                 width: int = 1920, height: int = 1080,
                 framerate: int = 10, logs_dir: Path | None = None):
        self.display = display
        self.output = output
        self.width = width
        self.height = height
        self.framerate = framerate
        self.logs_dir = logs_dir or output.parent
        self._proc: subprocess.Popen | None = None
        self._start_monotonic: float | None = None

    def start(self) -> RecorderHandle:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not installed")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log = open(self.logs_dir / "ffmpeg_record.log", "ab")
        cmd = [
            ffmpeg, "-y",
            "-f", "x11grab",
            "-framerate", str(self.framerate),
            "-video_size", f"{self.width}x{self.height}",
            "-i", self.display,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-tune", "zerolatency",
            str(self.output),
        ]
        env = {**os.environ, "DISPLAY": self.display}
        self._start_monotonic = time.monotonic()
        self._proc = subprocess.Popen(
            cmd, stdout=log, stderr=log, env=env, start_new_session=True,
            stdin=subprocess.PIPE,
        )
        # Give ffmpeg ~0.5s to initialise its capture pipeline.
        time.sleep(0.5)
        return RecorderHandle(
            output_path=self.output,
            pid=self._proc.pid,
            start_monotonic=self._start_monotonic,
        )

    def stop(self, timeout: float = 8.0) -> Path:
        if not self._proc:
            return self.output
        if self._proc.poll() is not None:
            return self.output
        # Send 'q' to ffmpeg's stdin for a clean shutdown (writes the moov
        # atom so the mp4 is playable). Fall back to SIGTERM/SIGKILL.
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.write(b"q")
                self._proc.stdin.flush()
                self._proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                self._proc.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        return self.output

    def elapsed(self) -> float:
        if self._start_monotonic is None:
            return 0.0
        return time.monotonic() - self._start_monotonic
