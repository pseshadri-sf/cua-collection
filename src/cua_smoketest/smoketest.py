from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .assets import AssetGenerator, GeneratedAssets
from .automation import FreeCADAutomation
from .display import DisplayManager, DisplaySession
from .environment import EnvironmentInspector, EnvironmentReport
from .logger import SmoketestLogger
from .paths import SmoketestPaths
from .screenshots import ScreenshotCapture


@dataclass
class SmoketestResult:
    paths: SmoketestPaths
    env: EnvironmentReport
    display: DisplaySession
    assets: GeneratedAssets
    screenshots: list[Path]
    log_path: Path
    success: bool
    error: str | None = None


class Smoketest:
    """Orchestrates the full FreeCAD GUI smoketest pipeline."""

    def __init__(self, paths: SmoketestPaths, generator_script: Path,
                 max_assets: int = 3):
        self.paths = paths
        self.generator_script = generator_script
        self.max_assets = max_assets

    def run(self) -> SmoketestResult:
        self.paths.ensure()
        logger = SmoketestLogger(self.paths.logs)

        inspector = EnvironmentInspector()
        env = inspector.inspect()
        logger.log.freecad_version = self._freecad_version(inspector, env)
        logger.log.blender_version = self._blender_version(env)
        logger.event("environment", **env.to_dict())

        display_mgr = DisplayManager(self.paths.logs)
        screenshots: list[Path] = []
        assets = GeneratedAssets(fcstd_files=[], step_files=[])
        success = False
        error: str | None = None
        session: DisplaySession | None = None
        automation: FreeCADAutomation | None = None

        try:
            session = display_mgr.acquire()
            logger.log.display = session.display
            logger.event("display_acquired", display=session.display, reused=session.reused)

            asset_gen = AssetGenerator(
                self.paths.assets, self.paths.logs,
                freecadcmd=inspector.freecadcmd_binary(env),
            )
            assets = asset_gen.generate(self.generator_script)
            logger.log.assets = [str(p) for p in assets.all_files]
            logger.event("assets_generated", count=len(assets.all_files))
            if len(assets.all_files) < 3:
                raise RuntimeError(
                    f"Expected at least 3 assets, got {len(assets.all_files)}"
                )

            capture = ScreenshotCapture(self.paths.screenshots)
            automation = FreeCADAutomation(
                session.display, self.paths.logs,
                freecad_binary=inspector.freecad_binary(env),
            )

            # 1) Launch FreeCAD bare and screenshot.
            launch = automation.launch(asset=None)
            logger.event("freecad_launched",
                         pid=launch.pid, window_id=launch.window_id,
                         window_title=launch.window_title)
            time.sleep(2.0)
            shot = capture.capture("01_freecad_launched.png")
            screenshots.append(shot.path)
            logger.event("screenshot", backend=shot.backend, path=str(shot.path))
            automation.quit()
            time.sleep(1.0)

            # 2) For each of up to N assets: launch with asset path & shoot.
            chosen = self._choose_assets(assets, self.max_assets)
            for idx, asset in enumerate(chosen, start=2):
                tag = f"{idx:02d}_loaded_{self._slug(asset)}.png"
                try:
                    launch = automation.launch(asset=asset)
                    logger.event("freecad_loaded_asset",
                                 asset=str(asset),
                                 window_id=launch.window_id,
                                 window_title=launch.window_title)
                    time.sleep(3.0)
                    automation.fit_view(window_id=launch.window_id)
                    time.sleep(1.0)
                    shot = capture.capture(tag)
                    screenshots.append(shot.path)
                    logger.event("screenshot",
                                 backend=shot.backend, asset=str(asset),
                                 path=str(shot.path))
                finally:
                    automation.quit()
                    time.sleep(1.0)

            success = bool(screenshots) and any(
                p.name.startswith("01_") for p in screenshots
            ) and any(
                p.name.startswith(("02_", "03_", "04_")) for p in screenshots
            )

        except Exception as exc:  # noqa: BLE001 - capture for the report
            error = f"{type(exc).__name__}: {exc}"
            logger.error(error)
            # Diagnostic screenshot if at all possible.
            if session is not None:
                try:
                    capture = ScreenshotCapture(self.paths.screenshots)
                    diag = capture.capture("99_diagnostic.png")
                    screenshots.append(diag.path)
                    logger.event("diagnostic_screenshot",
                                 backend=diag.backend, path=str(diag.path))
                except Exception as inner:  # noqa: BLE001
                    logger.error(f"diagnostic-screenshot failed: {inner}")
        finally:
            if automation is not None:
                automation.quit()
            if session is not None and not session.reused:
                display_mgr.release()
            logger.log.screenshots = [str(p) for p in screenshots]
            log_path = logger.write()

        return SmoketestResult(
            paths=self.paths, env=env, display=session or DisplaySession("", None, None, False),
            assets=assets, screenshots=screenshots, log_path=log_path,
            success=success, error=error,
        )

    @staticmethod
    def _choose_assets(assets: GeneratedAssets, n: int) -> list[Path]:
        # Prefer one .FCStd + then .step files for visual variety.
        chosen: list[Path] = []
        if assets.fcstd_files:
            chosen.append(assets.fcstd_files[0])
        for s in assets.step_files:
            if len(chosen) >= n:
                break
            chosen.append(s)
        for f in assets.fcstd_files[1:]:
            if len(chosen) >= n:
                break
            chosen.append(f)
        return chosen[:n]

    @staticmethod
    def _slug(path: Path) -> str:
        return f"{path.stem.lower()}_{path.suffix.lstrip('.').lower()}"

    @staticmethod
    def _first_matching_line(text: str, needle: str) -> str:
        for line in text.splitlines():
            if needle.lower() in line.lower():
                return line.strip()
        return text.strip().splitlines()[0] if text.strip() else ""

    def _freecad_version(self, inspector: EnvironmentInspector,
                         env: EnvironmentReport) -> str:
        binary = inspector.freecadcmd_binary(env)
        if not binary:
            return ""
        try:
            out = subprocess.run([binary, "--version"], capture_output=True,
                                 text=True, timeout=10)
            return self._first_matching_line(out.stdout + "\n" + out.stderr, "FreeCAD")
        except Exception:  # noqa: BLE001
            return ""

    def _blender_version(self, env: EnvironmentReport) -> str:
        binary = env.tools.get("blender") or shutil.which("blender")
        if not binary:
            return ""
        try:
            out = subprocess.run([binary, "--version"], capture_output=True,
                                 text=True, timeout=15)
            return self._first_matching_line(out.stdout + "\n" + out.stderr, "Blender")
        except Exception:  # noqa: BLE001
            return ""
