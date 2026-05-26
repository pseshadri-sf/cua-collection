from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SmoketestPaths:
    root: Path
    assets: Path
    screenshots: Path
    logs: Path
    scripts: Path

    @classmethod
    def default(cls) -> "SmoketestPaths":
        return cls.for_app("gui")

    @classmethod
    def for_app(cls, app_name: str) -> "SmoketestPaths":
        """Runtime tree per target app — e.g. for_app('blender') -> ~/cua_blender_smoketest."""
        root = Path.home() / f"cua_{app_name}_smoketest"
        return cls(
            root=root,
            assets=root / "assets",
            screenshots=root / "screenshots",
            logs=root / "logs",
            scripts=root / "scripts",
        )

    def ensure(self) -> None:
        for p in (self.root, self.assets, self.screenshots, self.logs, self.scripts):
            p.mkdir(parents=True, exist_ok=True)
