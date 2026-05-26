from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GeneratedAssets:
    fcstd_files: list[Path]
    step_files: list[Path]

    @property
    def all_files(self) -> list[Path]:
        return [*self.fcstd_files, *self.step_files]


class AssetGenerator:
    """Generates a small set of FreeCAD-compatible CAD samples via freecadcmd.

    Produces deterministic, tiny .FCStd and .step files for box, cylinder, bracket.
    """

    def __init__(self, assets_dir: Path, logs_dir: Path,
                 freecadcmd: str | None = None):
        self.assets_dir = assets_dir
        self.logs_dir = logs_dir
        self.freecadcmd = freecadcmd or shutil.which("freecadcmd") or shutil.which("FreeCADCmd")

    def generate(self, generator_script: Path) -> GeneratedAssets:
        if not self.freecadcmd:
            raise RuntimeError("freecadcmd binary not found; cannot generate assets")
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.logs_dir / "asset_generation.log"
        env = os.environ.copy()
        env["FREECAD_ASSET_DIR"] = str(self.assets_dir)
        with open(log_path, "ab") as log:
            result = subprocess.run(
                [self.freecadcmd, str(generator_script)],
                stdout=log, stderr=subprocess.STDOUT, timeout=180, env=env,
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"freecadcmd asset generation failed (rc={result.returncode}); see {log_path}"
            )
        return self.discover()

    def discover(self) -> GeneratedAssets:
        fcstd = sorted(self.assets_dir.glob("*.FCStd"))
        step = sorted(self.assets_dir.glob("*.step")) + sorted(self.assets_dir.glob("*.stp"))
        return GeneratedAssets(fcstd_files=fcstd, step_files=step)
