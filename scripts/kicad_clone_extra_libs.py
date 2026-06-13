"""Clone third-party KiCad libraries referenced by multiple broken boards.

Profile of the 22 score=0 boards on the 50-board ablation (2026-06-12) showed
several shared third-party libraries that no single board's repo ships, but
that multiple boards reference:

  - otter (Jana-Marie/otter):       68 fp across 2 boards (analog-toolkit,
                                    OtterPill, possibly more in the full set)
  - Mlab_* (mlab-modules/Modules):  ~120 fp across 2 boards (ISM01, RFSWITCH01)
  - digikey-footprints (Digi-Key/   5 fp across 2 boards (ESP32S3-DEVKIT-MINI,
    digikey-kicad-library):         KiCad_TopiBadge)

This script clones each to <cache>/<slug>. To register them with the
reconstruct subprocess, pass the resulting dirs via KICAD_EXTRA_LIB_ROOTS
(the existing reconstruct shim already scans these for .pretty subdirs).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Third-party libs referenced by 2+ broken boards. Verified URLs at time of
# writing; if a clone fails the rescue ablation will simply skip those .pretty
# subdirs and the affected boards stay broken on lib coverage alone.
EXTRA_LIBS = [
    # otter — Jana-Marie's PCB footprint collection (analog-toolkit, OtterPill)
    ("otter",               "https://github.com/Jana-Marie/otter"),
    # mlab — Modules KiCad library (ISM01, RFSWITCH01)
    ("mlab-modules",        "https://github.com/MLAB-project/Modules"),
    # Digi-Key official KiCad library (KiCad_TopiBadge, ESP32S3-DEVKIT-MINI)
    ("digikey-kicad-library", "https://github.com/Digi-Key/digikey-kicad-library"),
]


def _clone(url: str, dst: Path) -> tuple[bool, str]:
    if dst.exists() and any(dst.iterdir()):
        return True, "already-cached"
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth=1", "--filter=blob:none",
           "--no-tags", "--single-branch", url, str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        if r.returncode == 0:
            return True, "cloned"
        return False, f"rc={r.returncode}: {r.stderr.strip()[:200]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _scan_pretty(d: Path) -> list[Path]:
    return [p for p in d.rglob("*.pretty") if p.is_dir()]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-root", type=Path, required=True)
    args = ap.parse_args(argv)
    args.cache_root.mkdir(parents=True, exist_ok=True)
    n_ok = n_fail = 0
    pretty_dirs: list[Path] = []
    for slug, url in EXTRA_LIBS:
        dst = args.cache_root / slug
        ok, msg = _clone(url, dst)
        ps = _scan_pretty(dst) if ok else []
        pretty_dirs += ps
        if ok: n_ok += 1
        else:  n_fail += 1
        print(f"  [{('OK ' if ok else 'FAIL')}] {slug:<24} -> "
              f"{len(ps)} .pretty dirs   ({msg})")
        for p in ps[:6]:
            print(f"      .pretty: {p.relative_to(dst)}")
    print(f"\n  cloned {n_ok}/{len(EXTRA_LIBS)}, found {len(pretty_dirs)} .pretty dirs total")
    # Print the suggested KICAD_EXTRA_LIB_ROOTS value
    roots = sorted({p.parent.as_posix() for p in pretty_dirs})
    if roots:
        print(f"\nUse:")
        print(f"  export KICAD_EXTRA_LIB_ROOTS=\"{':'.join(roots)}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
