"""Reconstruct an agent's FreeCAD scene from its emitted Python chunks.

freecadcmd treats positional args as files-to-open (not script args), so
arguments are passed via env vars instead:

    FCRECON_CHUNKS=<chunks.json>
    FCRECON_OUT=<agent_model.FCStd>
    FCRECON_STATS=<stats.json>
    freecadcmd freecad_reconstruct.py

Input JSON is a list of strings (the agent's `type`/`python_eval` text).
Each chunk is exec()'d in a namespace pre-populated with App / Part / Gui
mock so chunks that reference Gui.* don't blow up headlessly.
"""
from __future__ import annotations

import json
import os
import sys
import traceback

import FreeCAD as App  # type: ignore[import-not-found]
import Part            # type: ignore[import-not-found]


def _stub_gui_module():
    """Pretend Gui exists; all calls are no-ops. The agent's Python
    sometimes references Gui.runCommand, Gui.activeDocument, etc.;
    we don't care about those for geometric replay."""
    class _Anything:
        def __init__(self, *_a, **_k):
            pass

        def __getattr__(self, _name):
            return _Anything()

        def __call__(self, *_a, **_k):
            return _Anything()

    return _Anything()


def run(argv: list[str]) -> int:
    chunks_path = os.environ.get("FCRECON_CHUNKS", "")
    out_path = os.environ.get("FCRECON_OUT", "")
    stats_path = os.environ.get("FCRECON_STATS", "")
    if not (chunks_path and out_path and stats_path):
        print("ERROR: FCRECON_CHUNKS, FCRECON_OUT, FCRECON_STATS env vars required",
              file=sys.stderr)
        return 2
    chunks = json.loads(open(chunks_path).read())

    Gui = _stub_gui_module()

    ns = {"App": App, "FreeCAD": App, "Part": Part, "Gui": Gui, "FreeCADGui": Gui}
    failures: list[str] = []
    for i, chunk in enumerate(chunks):
        try:
            exec(chunk, ns)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"chunk {i}: {type(exc).__name__}: {exc}")

    # Pick the active document if one exists; otherwise the newest one.
    doc = App.ActiveDocument
    if doc is None and App.listDocuments():
        doc = App.listDocuments()[next(iter(App.listDocuments()))]

    stats = _measure_doc(doc)
    stats["chunks"] = len(chunks)
    stats["replay_failures"] = failures[:20]

    if doc is not None:
        try:
            doc.saveAs(out_path)
        except Exception as exc:  # noqa: BLE001
            stats.setdefault("error", f"save failed: {exc}")

    open(stats_path, "w").write(json.dumps(stats, indent=2))
    return 0


def _measure_doc(doc) -> dict:
    if doc is None:
        return {
            "found": False, "object_count": 0, "object_names": [],
            "volume": 0.0, "surface_area": 0.0, "bbox": [0, 0, 0],
            "face_count": 0, "edge_count": 0, "vertex_count": 0,
            "error": "no document",
        }
    feat_objs = [o for o in doc.Objects if hasattr(o, "Shape") and o.Shape and not o.Shape.isNull()]
    names = [o.Name for o in feat_objs]
    if not feat_objs:
        return {
            "found": False, "object_count": 0, "object_names": [o.Name for o in doc.Objects],
            "volume": 0.0, "surface_area": 0.0, "bbox": [0, 0, 0],
            "face_count": 0, "edge_count": 0, "vertex_count": 0,
        }
    # Fuse all top-level shapes for aggregate metrics; protects against
    # partial constructions that left several disconnected pieces.
    shapes = [o.Shape for o in feat_objs]
    try:
        if len(shapes) == 1:
            combined = shapes[0]
        else:
            combined = shapes[0]
            for s in shapes[1:]:
                combined = combined.fuse(s)
    except Exception:  # noqa: BLE001
        combined = shapes[0]
    bb = combined.BoundBox
    return {
        "found": True,
        "object_count": len(feat_objs),
        "object_names": names,
        "volume": float(combined.Volume),
        "surface_area": float(combined.Area),
        "bbox": [float(bb.XLength), float(bb.YLength), float(bb.ZLength)],
        "face_count": len(combined.Faces),
        "edge_count": len(combined.Edges),
        "vertex_count": len(combined.Vertexes),
    }


if __name__ == "__main__" or "FreeCAD" in sys.modules:  # AppImage freecadcmd runs scripts with __name__=stem
    raise SystemExit(run(sys.argv))
