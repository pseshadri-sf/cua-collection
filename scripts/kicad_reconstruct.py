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
    # Fix #2: search EXTRA library roots beyond /usr/share. Allow caller to
    # extend via KICAD_EXTRA_LIB_ROOTS (colon-separated). Defaults include
    # SparkFun (covers the biggest missing-lib group: SparkFun-Resistor,
    # -Connector, -Capacitor, -Jumper, etc. — ~1,500 occurrences across the
    # 100-board collection).
    import glob as _glob
    _DEFAULT_EXTRA_ROOTS = [
        "/home/ubuntu/sparkfun-kicad/footprints",  # SparkFun official
    ]
    _extra = os.environ.get("KICAD_EXTRA_LIB_ROOTS", "")
    _roots = ["/usr/share/kicad/footprints"] + _DEFAULT_EXTRA_ROOTS
    if _extra:
        _roots += [p for p in _extra.split(":") if p]
    # Per-nickname lookup ('LibNickname' -> '/path/to/LibNickname.pretty') and
    # per-name fallback ('R_0805' -> path of first .pretty with that footprint).
    _lib_by_nick: dict[str, str] = {}
    _name_index: dict[str, str] = {}
    for _root in _roots:
        for _d in _glob.glob(_root + "/*.pretty"):
            _nick = os.path.basename(_d)[:-len(".pretty")]
            _lib_by_nick.setdefault(_nick, _d)
            for _mod in _glob.glob(_d + "/*.kicad_mod"):
                _name_index.setdefault(os.path.basename(_mod)[:-10], _d)
    _orig_fpl = pcbnew.FootprintLoad

    def _safe_fpl(lib, name, *a, **k):  # noqa: ANN001
        # 1) try the lib path the caller asked for verbatim
        try:
            fp = _orig_fpl(lib, name, *a, **k)
            if fp is not None:
                return fp
        except Exception:
            pass
        # 2) try the library NICKNAME extracted from the path (e.g. caller passed
        #    '/usr/share/kicad/footprints/SparkFun-Resistor.pretty' but the system
        #    doesn't have it — find SparkFun-Resistor.pretty in any registered root)
        try:
            nick = os.path.basename(lib.rstrip("/")) if lib else ""
            if nick.endswith(".pretty"):
                nick = nick[:-len(".pretty")]
            alt_lib = _lib_by_nick.get(nick)
            if alt_lib and alt_lib != lib:
                try:
                    fp = _orig_fpl(alt_lib, name, *a, **k)
                    if fp is not None:
                        return fp
                except Exception: pass
        except Exception: pass
        # 3) last-ditch: name-only fallback across all registered roots
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
