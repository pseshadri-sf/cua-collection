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
    # Fix #3: project-embedded footprints. KIRECON_PROJECT_LIBS_ROOT points to
    # a directory (typically a fresh shallow clone of the goal board's source
    # repo) that we scan for .pretty subdirs OR loose .kicad_mod files (some
    # projects ship loose modules in a `lib/` dir without the .pretty wrapper —
    # urchin pattern). For both we register a virtual root so the agent's
    # cached chunks resolve project libs (otter, urchin-footprints,
    # acheron_MX_SolderMask) without modification.
    # Coverage caveat (measured 2026-06-12): 17 low-availability boards have
    # repos but only 4 ship usable .pretty dirs; the rest reference upstream
    # commons (Acheron, Otter, SparkFun project variants).
    _proj = os.environ.get("KIRECON_PROJECT_LIBS_ROOT", "")
    if _proj and os.path.isdir(_proj):
        # 1. any .pretty under the tree (proper KiCad layout)
        for _ppretty in _glob.glob(_proj + "/**/*.pretty", recursive=True):
            if os.path.isdir(_ppretty):
                _roots.append(os.path.dirname(_ppretty))
        # NOTE: an earlier attempt synthesised a .pretty wrapper around loose
        # .kicad_mod files (e.g. urchin's `/lib/*.kicad_mod`) but pcbnew's SWIG
        # FootprintLoad segfaults on legacy-format modules wrapped this way
        # (PROPERTY_ENUM asserts in the constructor). The synthesised wrapper
        # is therefore DISABLED; if a project ships loose mods, the agent's
        # plan won't reach them — they'd need a separate offline-conversion
        # pass to KiCad-7+ format first.
    # Per-nickname lookup ('LibNickname' -> '/path/to/LibNickname.pretty').
    #
    # Two classes of segfault we have to defend against (proved via NerdNOS
    # reproducer on 2026-06-13, rc=139 inside pcbnew SWIG):
    #
    # 1. Name-fallback (removed): the earlier shim built a per-name index by
    #    globbing ~16k *.kicad_mod files and added a third fallback layer. With
    #    it present, reconstruct crashed silently and the agent board was
    #    never saved; with it removed the same chunk runs cleanly to 42/48
    #    footprints placed. The fallback covered a thin tail (boards that
    #    specified just a name with the wrong lib) and was the cause of the
    #    "broken cluster" of 22 boards stuck at score=0 on the 50-board sweep.
    #
    # 2. Risky .pretty dirs: not just legacy `(module` format, but ALSO some
    #    modern `(footprint`-format dirs (e.g. NerdNOS's bitaxe.pretty) whose
    #    files crash pcbnew SWIG on load. The crash is non-deterministic and
    #    file-internal. Cheap text inspection can't distinguish them; the only
    #    reliable test is to actually attempt a FootprintLoad. We probe each
    #    new .pretty candidate by trying the first .kicad_mod in a SACRIFICIAL
    #    SUBPROCESS — if the probe segfaults (rc not 0), skip the whole dir.
    #
    # System dirs (/usr/share + SparkFun) are TRUSTED (validated by upstream)
    # so we register them without probing — saves ~200 fork()/exec()s on every
    # reconstruct.
    # Simple legacy detector: skip .pretty whose first *.kicad_mod is the
    # KiCad 5 `(module ...)` form (SWIG asserts on those). NOT exhaustive —
    # some modern `(footprint ...)` dirs (e.g. NerdNOS's bitaxe.pretty) also
    # segfault on load, but we can't detect those without an in-process probe,
    # and a subprocess probe inherits the corrupted pcbnew state. The
    # ablation calls reconstruct in its own subprocess, so a crash here just
    # drops that one board to score=0 — the same behaviour as before any of
    # this rescue work, only without the silent name-fallback corruption.
    def _is_legacy_pretty(d: str) -> bool:
        mods = _glob.glob(d + "/*.kicad_mod")
        if not mods:
            return False
        try:
            with open(mods[0], "rb") as fh:
                return fh.read(12).lstrip().startswith(b"(module")
        except OSError:
            return True
    _lib_by_nick: dict[str, str] = {}
    _skipped_legacy: list[str] = []
    for _root in _roots:
        for _d in _glob.glob(_root + "/*.pretty"):
            if _is_legacy_pretty(_d):
                _skipped_legacy.append(_d)
                continue
            _nick = os.path.basename(_d)[:-len(".pretty")]
            _lib_by_nick.setdefault(_nick, _d)
    if _skipped_legacy:
        print(f"[reconstruct] skipped {len(_skipped_legacy)} legacy-format .pretty",
              file=sys.stderr)
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
