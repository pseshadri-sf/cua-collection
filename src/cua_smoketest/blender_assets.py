from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BlenderAssets:
    files: list[Path]

    @property
    def all_files(self) -> list[Path]:
        return list(self.files)


class BlenderAssetGenerator:
    """Drives `blender -b -P <script>` to produce small `.blend` samples.

    The script receives the output directory via the `BLENDER_ASSET_DIR`
    env var (Blender, like freecadcmd, treats positional args as files
    to open rather than as script arguments).
    """

    def __init__(self, assets_dir: Path, logs_dir: Path,
                 blender: str | None = None):
        self.assets_dir = assets_dir
        self.logs_dir = logs_dir
        self.blender = blender or shutil.which("blender")

    def generate(self, generator_script: Path) -> BlenderAssets:
        if not self.blender:
            raise RuntimeError("blender binary not found")
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.logs_dir / "blender_asset_generation.log"
        env = os.environ.copy()
        env["BLENDER_ASSET_DIR"] = str(self.assets_dir)
        # Software rendering — no GPU on this box.
        env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
        with open(log_path, "ab") as log:
            result = subprocess.run(
                [self.blender, "-b", "-P", str(generator_script)],
                stdout=log, stderr=subprocess.STDOUT, timeout=600, env=env,
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"blender asset generation failed (rc={result.returncode}); "
                f"see {log_path}"
            )
        return self.discover()

    def discover(self) -> BlenderAssets:
        return BlenderAssets(files=sorted(self.assets_dir.glob("*.blend")))
