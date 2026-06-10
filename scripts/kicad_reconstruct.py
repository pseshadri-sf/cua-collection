"""Reconstruct an agent's KiCad board from its emitted pcbnew_eval chunks.

Run with the system python3 that has the pcbnew module (KiCad install):

    KIRECON_CHUNKS=<chunks.json> KIRECON_SEED=<blank.kicad_pcb> \
    KIRECON_OUT=<agent_model.kicad_pcb> KIRECON_STATS=<stats.json> \
    python3 kicad_reconstruct.py

Input JSON is a list of strings (the agent's pcbnew_eval code blocks). The
chunks call `pcbnew.GetBoard()` to get the live GUI board; headlessly there is
no GUI board, so we LoadBoard(seed) and shim `pcbnew.GetBoard` to return it,
then exec each chunk against it. pcbnew.Refresh() and other GUI-only calls are
stubbed to no-ops. Mirrors freecad_reconstruct.py.
"""
from __future__ import annotations

import json
import os
import sys

# Reuse the shared measurement so goal + agent boards are measured identically.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kicad_measure import measure_board  # noqa: E402


def run() -> int:
    chunks_path = os.environ.get("KIRECON_CHUNKS", "")
    seed_path = os.environ.get("KIRECON_SEED", "")
    out_path = os.environ.get("KIRECON_OUT", "")
    stats_path = os.environ.get("KIRECON_STATS", "")
    if not (chunks_path and seed_path and out_path and stats_path):
        print("ERROR: KIRECON_CHUNKS, KIRECON_SEED, KIRECON_OUT, KIRECON_STATS required",
              file=sys.stderr)
        return 2
    import pcbnew  # type: ignore[import-not-found]
    chunks = json.loads(open(chunks_path).read())

    board = pcbnew.LoadBoard(seed_path)
    # Shim the GUI hooks: GetBoard -> our loaded board; Refresh -> no-op.
    pcbnew.GetBoard = lambda _b=board: _b
    pcbnew.Refresh = lambda *_a, **_k: None
    # FootprintLoad RAISES (not returns None) when the library path doesn't
    # exist — i.e. custom/project footprints not in the system install. Make it
    # return None instead so a missing footprint is skipped, not fatal, even if
    # the agent code doesn't guard the call itself. PLUS a system-wide fallback:
    # many boards reference a footprint whose NAME exists in the standard KiCad
    # library under a different library nickname — if the named lib fails, search
    # all installed .pretty dirs for "<name>.kicad_mod" and load the first match.
    import glob as _glob
    _fp_root = "/usr/share/kicad/footprints"
    _name_index: dict[str, str] = {}
    for _d in _glob.glob(_fp_root + "/*.pretty"):
        for _mod in _glob.glob(_d + "/*.kicad_mod"):
            _name_index.setdefault(os.path.basename(_mod)[:-10], _d)  # name -> lib dir
    _orig_fpl = pcbnew.FootprintLoad

    def _safe_fpl(lib, name, *a, **k):  # noqa: ANN001
        try:
            fp = _orig_fpl(lib, name, *a, **k)
            if fp is not None:
                return fp
        except Exception:
            pass
        alt = _name_index.get(name)
        if alt:
            try:
                return _orig_fpl(alt, name, *a, **k)
            except Exception:
                return None
        return None
    pcbnew.FootprintLoad = _safe_fpl

    ns = {"pcbnew": pcbnew}
    failures: list[str] = []
    for i, chunk in enumerate(chunks):
        try:
            exec(chunk, ns)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"chunk {i}: {type(exc).__name__}: {exc}")

    try:
        pcbnew.SaveBoard(out_path, board)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"save: {type(exc).__name__}: {exc}")

    stats = measure_board(board)
    stats["chunks"] = len(chunks)
    stats["replay_failures"] = failures[:20]
    open(stats_path, "w").write(json.dumps(stats, indent=2))
    print(f"[ok] reconstruct: {stats.get('footprint_count')} fp, "
          f"{len(failures)} replay failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
