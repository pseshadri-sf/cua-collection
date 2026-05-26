from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .automation import FreeCADAutomation, FreeCADLaunch


@dataclass
class CursorOpenResult:
    asset: Path
    window_id: str | None
    window_title: str | None


class CursorFreeCADAutomation:
    """Drives FreeCAD with **actual cursor movement** via pyautogui.

    Contrast with FreeCADAutomation: that class restarts FreeCAD for every
    asset and uses xdotool keystrokes only. This class:

      * Launches FreeCAD **once**, keeps the process alive.
      * For each asset, physically moves the mouse cursor to the File menu,
        clicks, navigates to the asset path via the Qt file dialog, and
        presses Enter — the same gestures a human would perform.
      * Uses pyautogui for mouse motion / clicks / typing, falling back to
        xdotool only for window activation (which has no cursor analog).
      * Closes the active document with Ctrl+W between assets, handling the
        "save changes?" prompt non-destructively.
    """

    # Approximate FreeCAD 0.19 menu coordinates on the 1920x1080 Xvfb display.
    FILE_MENU_XY = (22, 10)         # File label in the menubar
    OPEN_ITEM_XY = (50, 58)         # "Open..." row in the File dropdown
    # Cursor "rest" position after each asset, well away from any chrome.
    REST_XY = (960, 540)

    def __init__(self, display: str, logs_dir: Path,
                 freecad_binary: str | None = None,
                 window_timeout: float = 90.0):
        self.display = display
        self.logs_dir = logs_dir
        self._inner = FreeCADAutomation(
            display=display, logs_dir=logs_dir,
            freecad_binary=freecad_binary, window_timeout=window_timeout,
        )
        self._launch: FreeCADLaunch | None = None
        self._pg = None  # imported lazily once DISPLAY is set

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> FreeCADLaunch:
        os.environ["DISPLAY"] = self.display
        self._pg = self._import_pyautogui()
        self._launch = self._inner.launch(asset=None)
        # FreeCAD spends a few seconds populating the Start page on first launch.
        time.sleep(4.0)
        return self._launch

    def quit(self) -> None:
        self._inner.quit()

    # --- per-asset actions -------------------------------------------------

    def open_asset(self, asset_path: Path,
                   load_timeout: float = 12.0) -> CursorOpenResult:
        if self._launch is None:
            raise RuntimeError("start() must be called before open_asset()")
        self._activate_freecad()

        pg = self._pg
        # 1. Move cursor visibly to the File menu and click.
        pg.moveTo(*self.FILE_MENU_XY, duration=0.25)
        pg.click()
        time.sleep(0.6)
        # 2. Move cursor down to the "Open..." row and click it. (Qt highlights
        #    on 'o' but doesn't activate — explicit click is more reliable.)
        pg.moveTo(*self.OPEN_ITEM_XY, duration=0.2)
        pg.click()
        time.sleep(1.5)
        # 3. Qt's file dialog opens with focus on the filename input. Select-all
        #    so any auto-filled value (e.g. last opened path) is replaced.
        pg.hotkey("ctrl", "a")
        time.sleep(0.1)
        pg.typewrite(str(asset_path), interval=0.005)
        time.sleep(0.3)
        pg.press("enter")
        # 4. Wait for the document tab to appear.
        time.sleep(load_timeout * 0.5)
        # Park the cursor away from menus so subsequent screenshots are clean.
        pg.moveTo(*self.REST_XY, duration=0.15)
        return CursorOpenResult(
            asset=asset_path,
            window_id=self._launch.window_id,
            window_title=self._launch.window_title,
        )

    def fit_view(self) -> None:
        # Re-use the xdotool fit-view path (works against the focused window).
        if self._launch is not None:
            self._inner.fit_view(window_id=self._launch.window_id)

    def close_active_document(self) -> None:
        """Close the front document without prompting via the menu accelerator."""
        pg = self._pg
        self._activate_freecad()
        # Ctrl+W closes the active MDI sub-window (the document tab).
        pg.hotkey("ctrl", "w")
        time.sleep(0.6)
        # If a "Save changes?" dialog appeared, choose "Discard" (Alt+D in Qt).
        pg.hotkey("alt", "d")
        time.sleep(0.4)
        pg.moveTo(*self.REST_XY, duration=0.1)

    # --- internals ---------------------------------------------------------

    def _activate_freecad(self) -> None:
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
        # pyautogui reads DISPLAY at import; ensure caller set it first.
        import pyautogui  # noqa: PLC0415 - intentional lazy import
        pyautogui.FAILSAFE = False  # corner-trigger pointless on Xvfb
        pyautogui.PAUSE = 0.05
        return pyautogui
