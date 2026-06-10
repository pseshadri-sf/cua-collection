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
