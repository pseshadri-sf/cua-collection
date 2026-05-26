from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .blender_assets import BlenderAssetGenerator, BlenderAssets
from .blender_automation import BlenderAutomation
from .blender_cursor_automation import BlenderCursorAutomation
from .display import DisplayManager, DisplaySession
from .environment import EnvironmentInspector, EnvironmentReport
from .logger import SmoketestLogger
from .paths import SmoketestPaths
from .screenshots import ScreenshotCapture


@dataclass
class BlenderSmoketestResult:
    paths: SmoketestPaths
    env: EnvironmentReport
    display: DisplaySession
    assets: BlenderAssets
    screenshots: list[Path]
    log_path: Path
    success: bool
    error: str | None = None


class BlenderSmoketest:
    """Blender counterpart of `Smoketest`. Same pipeline shape, Blender-specific
    automation classes.
    """

    def __init__(self, paths: SmoketestPaths, generator_script: Path,
                 max_assets: int = 20,
                 mode: str = "process",
                 skip_generation: bool = False,
                 screenshot_prefix: str = "",
                 asset_offset: int = 0):
        if mode not in ("process", "cursor"):
            raise ValueError(f"unknown mode: {mode}")
        self.paths = paths
        self.generator_script = generator_script
        self.max_assets = max_assets
        self.mode = mode
        self.skip_generation = skip_generation
        self.screenshot_prefix = screenshot_prefix
        self.asset_offset = asset_offset

    def run(self) -> BlenderSmoketestResult:
        self.paths.ensure()
        logger = SmoketestLogger(self.paths.logs, name="blender_smoketest")

        inspector = EnvironmentInspector()
        env = inspector.inspect()
        logger.log.blender_version = self._blender_version(env)
        logger.event("environment", **env.to_dict())

        display_mgr = DisplayManager(self.paths.logs)
        screenshots: list[Path] = []
        assets = BlenderAssets(files=[])
        success = False
        error: str | None = None
        session: DisplaySession | None = None

        try:
            session = display_mgr.acquire()
            logger.log.display = session.display
            logger.event("display_acquired",
                         display=session.display, reused=session.reused)

            asset_gen = BlenderAssetGenerator(
                self.paths.assets, self.paths.logs,
                blender=env.tools.get("blender") or shutil.which("blender"),
            )
            if self.skip_generation:
                assets = asset_gen.discover()
                logger.event("assets_generation_skipped",
                             existing=len(assets.all_files))
            else:
                assets = asset_gen.generate(self.generator_script)
                logger.event("assets_generated", count=len(assets.all_files))

            logger.log.assets = [str(p) for p in assets.all_files]
            if len(assets.all_files) < 3:
                raise RuntimeError(
                    f"Expected at least 3 assets, got {len(assets.all_files)}"
                )

            capture = ScreenshotCapture(self.paths.screenshots)
            chosen = assets.all_files[self.asset_offset:
                                       self.asset_offset + self.max_assets]
            logger.event("asset_selection",
                         count=len(chosen), offset=self.asset_offset,
                         max_assets=self.max_assets)

            if self.mode == "process":
                self._run_process_mode(
                    chosen=chosen, capture=capture, env=env,
                    screenshots=screenshots, logger=logger,
                )
            else:
                self._run_cursor_mode(
                    session=session, chosen=chosen, capture=capture, env=env,
                    screenshots=screenshots, logger=logger,
                )

            launched_prefix = f"{self.screenshot_prefix}01_"
            loaded_any = any(
                p.name.startswith(self.screenshot_prefix) and "_loaded_" in p.name
                for p in screenshots
            )
            success = (
                bool(screenshots)
                and any(p.name.startswith(launched_prefix) for p in screenshots)
                and loaded_any
            )
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            logger.error(error)
            if session is not None:
                try:
                    capture = ScreenshotCapture(self.paths.screenshots)
                    diag = capture.capture(
                        f"{self.screenshot_prefix}99_diagnostic.png"
                    )
                    screenshots.append(diag.path)
                    logger.event("diagnostic_screenshot",
                                 backend=diag.backend, path=str(diag.path))
                except Exception as inner:  # noqa: BLE001
                    logger.error(f"diagnostic-screenshot failed: {inner}")
        finally:
            if session is not None and not session.reused:
                display_mgr.release()
            logger.log.screenshots = [str(p) for p in screenshots]
            log_path = logger.write()

        return BlenderSmoketestResult(
            paths=self.paths, env=env,
            display=session or DisplaySession("", None, None, False),
            assets=assets, screenshots=screenshots, log_path=log_path,
            success=success, error=error,
        )

    # --- mode implementations ---------------------------------------------

    def _run_process_mode(self, *, chosen: list[Path],
                          capture: ScreenshotCapture,
                          env: EnvironmentReport,
                          screenshots: list[Path],
                          logger: SmoketestLogger) -> None:
        automation = BlenderAutomation(
            os.environ.get("DISPLAY", ""), self.paths.logs,
            blender_binary=env.tools.get("blender") or shutil.which("blender"),
        )
        # 1) Bare launch screenshot.
        launch = automation.launch(asset=None)
        logger.event("blender_launched", pid=launch.pid,
                     window_id=launch.window_id, window_title=launch.window_title,
                     mode="process")
        time.sleep(3.0)
        automation.dismiss_splash()
        time.sleep(0.5)
        shot = capture.capture(f"{self.screenshot_prefix}01_blender_launched.png")
        screenshots.append(shot.path)
        logger.event("screenshot", backend=shot.backend, path=str(shot.path))
        automation.quit()
        time.sleep(1.0)

        # 2) Per-asset restart.
        for idx, asset in enumerate(chosen, start=2):
            tag = f"{self.screenshot_prefix}{idx:02d}_loaded_{asset.stem}.png"
            try:
                launch = automation.launch(asset=asset)
                logger.event("blender_loaded_asset", asset=str(asset),
                             window_id=launch.window_id,
                             window_title=launch.window_title)
                # Splash doesn't usually appear when a file is opened from CLI,
                # but send Escape defensively in case it does.
                time.sleep(3.0)
                automation.dismiss_splash()
                automation.fit_view(window_id=launch.window_id)
                time.sleep(0.6)
                shot = capture.capture(tag)
                screenshots.append(shot.path)
                logger.event("screenshot", backend=shot.backend,
                             asset=str(asset), path=str(shot.path))
            finally:
                automation.quit()
                time.sleep(1.0)

    def _run_cursor_mode(self, *, session: DisplaySession,
                         chosen: list[Path],
                         capture: ScreenshotCapture,
                         env: EnvironmentReport,
                         screenshots: list[Path],
                         logger: SmoketestLogger) -> None:
        cursor = BlenderCursorAutomation(
            display=session.display, logs_dir=self.paths.logs,
            blender_binary=env.tools.get("blender") or shutil.which("blender"),
        )
        try:
            launch = cursor.start()
            logger.event("blender_launched", pid=launch.pid,
                         window_id=launch.window_id,
                         window_title=launch.window_title, mode="cursor")
            shot = capture.capture(f"{self.screenshot_prefix}01_blender_launched.png")
            screenshots.append(shot.path)
            logger.event("screenshot", backend=shot.backend, path=str(shot.path))

            for idx, asset in enumerate(chosen, start=2):
                tag = f"{self.screenshot_prefix}{idx:02d}_loaded_{asset.stem}.png"
                try:
                    cursor.open_asset(asset)
                    logger.event("cursor_opened_asset", asset=str(asset))
                    cursor.fit_view()
                    time.sleep(0.6)
                    shot = capture.capture(tag)
                    screenshots.append(shot.path)
                    logger.event("screenshot", backend=shot.backend,
                                 asset=str(asset), path=str(shot.path))
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"cursor open failed for {asset.name}: {exc}")
        finally:
            cursor.quit()

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _blender_version(env: EnvironmentReport) -> str:
        binary = env.tools.get("blender") or shutil.which("blender")
        if not binary:
            return ""
        try:
            out = subprocess.run([binary, "--version"], capture_output=True,
                                 text=True, timeout=15)
            for line in (out.stdout + "\n" + out.stderr).splitlines():
                if "blender" in line.lower():
                    return line.strip()
            return ""
        except Exception:  # noqa: BLE001
            return ""
