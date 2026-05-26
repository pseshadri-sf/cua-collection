from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .assets import AssetGenerator, GeneratedAssets
from .automation import FreeCADAutomation
from .cursor_automation import CursorFreeCADAutomation
from .display import DisplayManager, DisplaySession
from .downloader import AssetDownloader
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
                 max_assets: int = 3, download_count: int = 0,
                 download_cache: Path | None = None,
                 screenshot_prefix: str = "",
                 exclude_basenames: set[str] | None = None,
                 mode: str = "process",
                 skip_generation: bool = False,
                 only_downloaded: bool = False):
        if mode not in ("process", "cursor"):
            raise ValueError(f"unknown mode: {mode}")
        self.paths = paths
        self.generator_script = generator_script
        self.max_assets = max_assets
        self.download_count = download_count
        self.download_cache = download_cache or (Path.home() / ".cache" / "freecad-library")
        self.screenshot_prefix = screenshot_prefix
        self.exclude_basenames = exclude_basenames or set()
        self.mode = mode
        self.skip_generation = skip_generation
        self.only_downloaded = only_downloaded

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

        try:
            session = display_mgr.acquire()
            logger.log.display = session.display
            logger.event("display_acquired", display=session.display, reused=session.reused)

            asset_gen = AssetGenerator(
                self.paths.assets, self.paths.logs,
                freecadcmd=inspector.freecadcmd_binary(env),
            )
            if self.skip_generation:
                assets = asset_gen.discover()
                logger.event("assets_generation_skipped", existing=len(assets.all_files))
            else:
                assets = asset_gen.generate(self.generator_script)
                logger.event("assets_generated", count=len(assets.all_files))

            downloaded: list[Path] = []
            if self.download_count > 0:
                downloader = AssetDownloader(
                    target_dir=self.paths.assets,
                    cache_dir=self.download_cache,
                    logs_dir=self.paths.logs,
                )
                try:
                    downloaded = downloader.download(
                        count=self.download_count,
                        exclude_basenames=self.exclude_basenames,
                    )
                    logger.event("assets_downloaded",
                                 count=len(downloaded),
                                 cache=str(self.download_cache))
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"download failed: {exc}")

            assets = asset_gen.discover()
            logger.log.assets = [str(p) for p in assets.all_files]
            if len(assets.all_files) < 3:
                raise RuntimeError(
                    f"Expected at least 3 assets, got {len(assets.all_files)}"
                )

            capture = ScreenshotCapture(self.paths.screenshots)
            if self.only_downloaded and downloaded:
                chosen = downloaded[: self.max_assets]
                logger.event("asset_selection", source="downloaded", count=len(chosen))
            else:
                chosen = self._choose_assets(assets, self.max_assets)
                logger.event("asset_selection", source="all", count=len(chosen))
            if self.mode == "process":
                self._run_process_mode(
                    chosen=chosen, capture=capture,
                    inspector=inspector, env=env,
                    screenshots=screenshots, logger=logger,
                )
            else:
                self._run_cursor_mode(
                    session=session, chosen=chosen, capture=capture,
                    inspector=inspector, env=env,
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
                    diag = capture.capture(f"{self.screenshot_prefix}99_diagnostic.png")
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

        return SmoketestResult(
            paths=self.paths, env=env, display=session or DisplaySession("", None, None, False),
            assets=assets, screenshots=screenshots, log_path=log_path,
            success=success, error=error,
        )

    # --- mode implementations --------------------------------------------

    def _run_process_mode(self, *, chosen: list[Path],
                          capture: ScreenshotCapture,
                          inspector: EnvironmentInspector,
                          env: EnvironmentReport,
                          screenshots: list[Path],
                          logger: SmoketestLogger) -> None:
        automation = FreeCADAutomation(
            os.environ.get("DISPLAY", ""), self.paths.logs,
            freecad_binary=inspector.freecad_binary(env),
        )
        # 1) Launch FreeCAD bare and screenshot.
        launch = automation.launch(asset=None)
        logger.event("freecad_launched", pid=launch.pid,
                     window_id=launch.window_id, window_title=launch.window_title)
        time.sleep(2.0)
        shot = capture.capture(f"{self.screenshot_prefix}01_freecad_launched.png")
        screenshots.append(shot.path)
        logger.event("screenshot", backend=shot.backend, path=str(shot.path))
        automation.quit()
        time.sleep(1.0)

        # 2) Restart FreeCAD per asset.
        for idx, asset in enumerate(chosen, start=2):
            tag = f"{self.screenshot_prefix}{idx:02d}_loaded_{self._slug(asset)}.png"
            try:
                launch = automation.launch(asset=asset)
                logger.event("freecad_loaded_asset", asset=str(asset),
                             window_id=launch.window_id, window_title=launch.window_title)
                time.sleep(3.0)
                automation.fit_view(window_id=launch.window_id)
                time.sleep(1.0)
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
                         inspector: EnvironmentInspector,
                         env: EnvironmentReport,
                         screenshots: list[Path],
                         logger: SmoketestLogger) -> None:
        cursor = CursorFreeCADAutomation(
            display=session.display, logs_dir=self.paths.logs,
            freecad_binary=inspector.freecad_binary(env),
        )
        try:
            launch = cursor.start()
            logger.event("freecad_launched", pid=launch.pid,
                         window_id=launch.window_id, window_title=launch.window_title,
                         mode="cursor")
            shot = capture.capture(f"{self.screenshot_prefix}01_freecad_launched.png")
            screenshots.append(shot.path)
            logger.event("screenshot", backend=shot.backend, path=str(shot.path))

            for idx, asset in enumerate(chosen, start=2):
                tag = f"{self.screenshot_prefix}{idx:02d}_loaded_{self._slug(asset)}.png"
                try:
                    cursor.open_asset(asset)
                    logger.event("cursor_opened_asset", asset=str(asset))
                    cursor.fit_view()
                    time.sleep(0.8)
                    shot = capture.capture(tag)
                    screenshots.append(shot.path)
                    logger.event("screenshot", backend=shot.backend,
                                 asset=str(asset), path=str(shot.path))
                except Exception as exc:  # noqa: BLE001 - continue with next asset
                    logger.error(f"cursor open failed for {asset.name}: {exc}")
                finally:
                    try:
                        cursor.close_active_document()
                    except Exception as exc:  # noqa: BLE001
                        logger.error(f"close_active_document failed: {exc}")
                        # If close hangs FreeCAD, restart it.
                        cursor.quit()
                        time.sleep(1.0)
                        launch = cursor.start()
                        logger.event("freecad_restarted", pid=launch.pid)
        finally:
            cursor.quit()

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _choose_assets(assets: GeneratedAssets, n: int) -> list[Path]:
        # Interleave .step and .FCStd so the early screenshots show variety
        # even when n is small. Deterministic by sorted path order.
        step = list(assets.step_files)
        fcstd = list(assets.fcstd_files)
        chosen: list[Path] = []
        while (step or fcstd) and len(chosen) < n:
            if step:
                chosen.append(step.pop(0))
            if len(chosen) >= n:
                break
            if fcstd:
                chosen.append(fcstd.pop(0))
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
