from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass, field


@dataclass
class EnvironmentReport:
    os_id: str
    os_version: str
    arch: str
    display: str
    has_nvidia: bool
    tools: dict[str, str | None] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class EnvironmentInspector:
    """Detects OS, architecture, display, GPU presence, and key CLI tools."""

    TOOLS = (
        "freecad", "FreeCAD", "freecadcmd", "FreeCADCmd",
        "blender", "Xvfb", "openbox", "xdotool", "wmctrl",
        "scrot", "import", "xdpyinfo", "xset",
    )

    def inspect(self) -> EnvironmentReport:
        os_id, os_version = self._detect_os()
        tools = {t: shutil.which(t) for t in self.TOOLS}
        return EnvironmentReport(
            os_id=os_id,
            os_version=os_version,
            arch=platform.machine(),
            display=os.environ.get("DISPLAY", ""),
            has_nvidia=self._has_nvidia(),
            tools=tools,
        )

    @staticmethod
    def _detect_os() -> tuple[str, str]:
        try:
            with open("/etc/os-release") as fh:
                data = {}
                for line in fh:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        data[k] = v.strip('"')
            return data.get("ID", "unknown"), data.get("VERSION_ID", "unknown")
        except OSError:
            return platform.system().lower(), platform.release()

    @staticmethod
    def _has_nvidia() -> bool:
        if not shutil.which("nvidia-smi"):
            return False
        result = subprocess.run(
            ["nvidia-smi"], capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0

    def freecad_binary(self, report: EnvironmentReport) -> str | None:
        for name in ("freecad", "FreeCAD"):
            if report.tools.get(name):
                return report.tools[name]
        return None

    def freecadcmd_binary(self, report: EnvironmentReport) -> str | None:
        for name in ("freecadcmd", "FreeCADCmd"):
            if report.tools.get(name):
                return report.tools[name]
        return None
