from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .blender_automation import BlenderAutomation, BlenderLaunch


@dataclass
class CursorOpenResult:
    asset: Path
    window_id: str | None
    window_title: str | None


class BlenderCursorAutomation:
    """Cursor-driven Blender driver via pyautogui.

    Single Blender process: launch once, then for each asset move the
    mouse to the File menu, click, click Open..., type the absolute
    path into Blender's file browser path bar, press Enter.

    Unlike FreeCAD (which is a multi-document app), Blender is
    single-document: opening a new file replaces the current scene,
    so there's no per-asset close step.
    """

    # Approximate Blender 3.0 menu coordinates on 1920x1080.
    FILE_MENU_XY = (50, 17)         # "File" label in the topbar (cursor lands here visibly)
    FILENAME_INPUT_XY = (700, 870)  # Filename text input near bottom of file browser
    REST_XY = (960, 540)            # Centre of 3D viewport

    def __init__(self, display: str, logs_dir: Path,
                 blender_binary: str | None = None,
                 window_timeout: float = 60.0):
        self.display = display
        self.logs_dir = logs_dir
        self._inner = BlenderAutomation(
            display=display, logs_dir=logs_dir,
            blender_binary=blender_binary, window_timeout=window_timeout,
        )
        self._launch: BlenderLaunch | None = None
        self._pg = None

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> BlenderLaunch:
        os.environ["DISPLAY"] = self.display
        self._pg = self._import_pyautogui()
        self._launch = self._inner.launch(asset=None)
        # Blender shows a splash overlay on launch — wait + Escape dismisses it.
        time.sleep(3.0)
        self._inner.dismiss_splash()
        time.sleep(0.5)
        return self._launch

    def quit(self) -> None:
        self._inner.quit()

    # --- per-asset action --------------------------------------------------

    def open_asset(self, asset_path: Path,
                   load_timeout: float = 8.0) -> CursorOpenResult:
        if self._launch is None:
            raise RuntimeError("start() must be called before open_asset()")
        self._activate_blender()
        pg = self._pg
        # 1. Move cursor visibly to the File menu (demonstrates cursor use).
        pg.moveTo(*self.FILE_MENU_XY, duration=0.25)
        time.sleep(0.4)
        # 2. Ctrl+O reliably opens the file browser — a Blender menubar click
        #    only "arms" the menu without dropping it down in this version.
        pg.hotkey("ctrl", "o")
        time.sleep(2.0)
        # 3. Click the filename input near the bottom of Blender's dialog
        #    and type the basename. Blender's filename input sanitises
        #    slashes to underscores, so a full path won't work — but the
        #    dialog opens to the previously-used directory, which is our
        #    assets dir after the first open.
        pg.moveTo(*self.FILENAME_INPUT_XY, duration=0.2)
        pg.click()
        time.sleep(0.3)
        pg.hotkey("ctrl", "a")
        time.sleep(0.1)
        pg.typewrite(asset_path.name, interval=0.005)
        time.sleep(0.3)
        # 4. Two Enters: the first commits the filename, the second activates
        #    the default Open button.
        pg.press("enter")
        time.sleep(0.4)
        pg.press("enter")
        time.sleep(load_timeout)
        # Park cursor in viewport so subsequent Home key lands there
        # (Blender uses hover-focus for area-scoped shortcuts).
        pg.moveTo(*self.REST_XY, duration=0.15)
        return CursorOpenResult(
            asset=asset_path,
            window_id=self._launch.window_id,
            window_title=self._launch.window_title,
        )

    def navigate_to_asset_dir(self, asset_dir: Path) -> None:
        """One-time navigation to seed Blender's "last-used directory" so
        the subsequent file dialogs default to `asset_dir`. Call this
        right after start() if the dialog might not already be there."""
        self._activate_blender()
        pg = self._pg
        pg.hotkey("ctrl", "o")
        time.sleep(2.0)
        pg.moveTo(*self.FILENAME_INPUT_XY, duration=0.2)
        pg.click()
        time.sleep(0.3)
        pg.hotkey("ctrl", "a")
        time.sleep(0.1)
        # Trailing slash tells Blender to navigate into the directory.
        # Slash-sanitisation still happens, so we type it segment by segment
        # using the up-directory shortcut. Easier: type the full path as the
        # filename — Blender treats absolute-looking paths with slashes as
        # navigation when there's no trailing filename. As a robust fallback,
        # press Escape to cancel after seeding history.
        pg.typewrite(str(asset_dir) + "/", interval=0.005)
        time.sleep(0.3)
        pg.press("enter")
        time.sleep(1.0)
        pg.press("escape")
        time.sleep(0.5)

    def fit_view(self) -> None:
        if self._launch is not None:
            self._inner.fit_view(window_id=self._launch.window_id)

    # --- internals ---------------------------------------------------------

    def _activate_blender(self) -> None:
        xdotool = shutil.which("xdotool")
        if not xdotool or self._launch is None or not self._launch.window_id:
            return
        env = {**os.environ, "DISPLAY": self.display}
        subprocess.run(
            [xdotool, "windowactivate", "--sync", self._launch.window_id],
            capture_output=True, env=env, timeout=5,
        )
        time.sleep(0.3)

    @staticmethod
    def _import_pyautogui():
        import pyautogui  # noqa: PLC0415
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.05
        return pyautogui
