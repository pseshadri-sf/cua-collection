from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ScreenshotResult:
    path: Path
    backend: str


class ScreenshotCapture:
    """Takes full-screen screenshots with multiple backends as fallbacks.

    Order: pyautogui -> scrot -> ImageMagick (`import -window root`).
    """

    def __init__(self, screenshots_dir: Path):
        self.screenshots_dir = screenshots_dir

    def capture(self, filename: str) -> ScreenshotResult:
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        target = self.screenshots_dir / filename
        errors: list[str] = []
        for backend in (self._pyautogui, self._scrot, self._imagemagick):
            try:
                if backend(target):
                    return ScreenshotResult(path=target, backend=backend.__name__.lstrip("_"))
            except Exception as exc:  # noqa: BLE001 - we want a clean fallback chain
                errors.append(f"{backend.__name__}: {exc}")
        raise RuntimeError(
            f"All screenshot backends failed for {target}: {'; '.join(errors) or 'no backends available'}"
        )

    @staticmethod
    def _pyautogui(target: Path) -> bool:
        if not os.environ.get("DISPLAY"):
            return False
        import pyautogui  # local import: needs DISPLAY set first
        img = pyautogui.screenshot()
        img.save(str(target))
        return target.exists() and target.stat().st_size > 0

    @staticmethod
    def _scrot(target: Path) -> bool:
        scrot = shutil.which("scrot")
        if not scrot:
            return False
        result = subprocess.run(
            [scrot, "--overwrite", str(target)],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0 and target.exists()

    @staticmethod
    def _imagemagick(target: Path) -> bool:
        imp = shutil.which("import")
        if not imp:
            return False
        result = subprocess.run(
            [imp, "-window", "root", str(target)],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0 and target.exists()
