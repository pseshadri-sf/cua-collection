from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class SmoketestLog:
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    display: str = ""
    freecad_version: str = ""
    blender_version: str = ""
    assets: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def event(self, name: str, **fields: Any) -> None:
        self.events.append({"t": datetime.now(timezone.utc).isoformat(), "name": name, **fields})

    def error(self, message: str) -> None:
        self.errors.append(message)
        self.event("error", message=message)


class SmoketestLogger:
    def __init__(self, logs_dir: Path, name: str = "smoketest"):
        self.logs_dir = logs_dir
        self.json_path = logs_dir / f"{name}.json"
        self.text_path = logs_dir / f"{name}.log"
        self.log = SmoketestLog()
        logs_dir.mkdir(parents=True, exist_ok=True)

    def write(self) -> Path:
        payload = {
            "timestamp": self.log.timestamp,
            "display": self.log.display,
            "freecad_version": self.log.freecad_version,
            "blender_version": self.log.blender_version,
            "assets": self.log.assets,
            "screenshots": self.log.screenshots,
            "events": self.log.events,
            "errors": self.log.errors,
        }
        self.json_path.write_text(json.dumps(payload, indent=2))
        return self.json_path

    def append_text(self, line: str) -> None:
        with open(self.text_path, "a") as fh:
            fh.write(line.rstrip("\n") + "\n")

    # Convenience proxies so callers can use logger.event / logger.error directly.
    def event(self, name: str, **fields: Any) -> None:
        self.log.event(name, **fields)

    def error(self, message: str) -> None:
        self.log.error(message)
