"""Thin entry point that delegates to the cua_smoketest package CLI."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure the src/ package is importable when run from the repo without install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cua_smoketest.cli import main  # noqa: E402


if __name__ == "__main__":
    generator = _REPO_ROOT / "scripts" / "generate_freecad_assets.py"
    argv = sys.argv[1:]
    if not any(a.startswith("--generator") for a in argv):
        argv = ["--generator", str(generator), *argv]
    raise SystemExit(main(argv))
