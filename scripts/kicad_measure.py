"""Load a .kicad_pcb board and emit its PCB-layout stats as JSON.

Run with the system python3 that has the pcbnew module (KiCad install):

    KIMEAS_ASSET=<board.kicad_pcb> KIMEAS_STATS=<stats.json> python3 kicad_measure.py

Stats are PCB-oriented (footprints/nets/tracks/outline), the inputs the KiCad
reconstruction metric (compare_kicad) scores. `measure_board` is reused by
kicad_reconstruct.py so the goal and the agent build are measured identically.
"""
from __future__ import annotations

import json
import os
import sys


def measure_board(board) -> dict:
    """PCB-layout stats for a loaded pcbnew BOARD."""
    import pcbnew  # type: ignore[import-not-found]
    tomm = pcbnew.ToMM

    footprints = []
    pad_count = 0
    for fp in board.GetFootprints():
        pos = fp.GetPosition()
        pads = list(fp.Pads())
        pad_count += len(pads)
        footprints.append({
            "ref": fp.GetReference(),
            "name": fp.GetFPIDAsString(),
            "at": [round(tomm(pos.x), 3), round(tomm(pos.y), 3)],
            "rot": round(fp.GetOrientationDegrees(), 1),
            "layer": "B.Cu" if fp.IsFlipped() else "F.Cu",
        })

    tracks = list(board.GetTracks())
    track_count = len(tracks)

    try:
        bb = board.GetBoardEdgesBoundingBox()
        if bb.GetWidth() == 0 and bb.GetHeight() == 0:
            bb = board.GetBoundingBox()
    except Exception:
        bb = board.GetBoundingBox()
    w_mm = round(tomm(bb.GetWidth()), 2)
    h_mm = round(tomm(bb.GetHeight()), 2)

    nets = []
    for code in range(1, board.GetNetCount()):
        try:
            ni = board.FindNet(code)
            if ni is not None and ni.GetNetname():
                nets.append(ni.GetNetname())
        except Exception:
            pass

    return {
        "found": bool(footprints or track_count),
        "footprint_count": len(footprints),
        "footprints": footprints,
        "net_count": max(0, board.GetNetCount() - 1),
        "nets": nets,
        "layer_count": board.GetCopperLayerCount(),
        "track_count": track_count,
        "outline_bbox_mm": [w_mm, h_mm],
        "pad_count": pad_count,
    }


def main() -> int:
    asset = os.environ.get("KIMEAS_ASSET", "")
    stats_path = os.environ.get("KIMEAS_STATS", "")
    if not (asset and stats_path):
        print("ERROR: KIMEAS_ASSET, KIMEAS_STATS env vars required", file=sys.stderr)
        return 2
    import pcbnew  # type: ignore[import-not-found]
    try:
        board = pcbnew.LoadBoard(asset)
        stats = measure_board(board)
    except Exception as exc:  # noqa: BLE001
        stats = {"found": False, "footprint_count": 0, "footprints": [],
                 "net_count": 0, "nets": [], "layer_count": 0, "track_count": 0,
                 "outline_bbox_mm": [0, 0], "pad_count": 0,
                 "error": f"{type(exc).__name__}: {exc}"}
    open(stats_path, "w").write(json.dumps(stats, indent=2))
    print(f"[ok] {stats_path}: {stats.get('footprint_count')} fp, "
          f"{stats.get('net_count')} nets, {stats.get('track_count')} tracks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
