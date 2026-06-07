"""Post-hoc geometric evaluator for agent trajectories.

For each trajectory.json:
  1. Extract the Python chunks the agent typed (`type` text on FreeCAD,
     `python_eval` code on Blender).
  2. Replay those chunks in a headless engine to reconstruct the agent's
     final document (.FCStd / .blend).
  3. Load the goal asset (original .step / .FCStd / .blend used to render
     the goal screenshot during smoketest).
  4. Compute geometric diff metrics between the two.

This is post-hoc — no harness changes needed; works against any
trajectory.json that already exists on disk.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# --- goal-asset resolution -------------------------------------------------

FREECAD_ASSETS_DIR = Path.home() / "cua_gui_smoketest" / "assets"
BLENDER_ASSETS_DIR = Path.home() / "cua_blender_smoketest" / "assets"


def _asset_from_sidecar(goal_png: Path) -> Path | None:
    """If a `<goal_stem>.meta.json` sidecar exists and names a real source
    asset, use it directly. Handles arbitrary goal naming (e.g. procured
    FX_/BX_ atlases) where the regex resolvers below don't apply."""
    for sc in (goal_png.with_suffix(".meta.json"),
               goal_png.parent / (goal_png.stem + ".meta.json")):
        if sc.exists():
            try:
                a = json.loads(sc.read_text()).get("asset")
                if a and Path(a).exists():
                    return Path(a)
            except Exception:
                pass
    return None


def resolve_freecad_goal_asset(goal_png: Path) -> Path | None:
    """Map a goal screenshot like `A_22_loaded_cylinder_step.png` back to
    its source asset under FREECAD_ASSETS_DIR (`cylinder.step`).
    """
    hit = _asset_from_sidecar(goal_png)
    if hit:
        return hit
    name = goal_png.name
    m = re.match(r"^[A-Z]_\d+_loaded_(.+?)_(step|stp|fcstd|brep|iges|igs|stl)\.png$",
                 name, re.IGNORECASE)
    if not m:
        return None
    base, ext = m.group(1), m.group(2).lower()
    ext = {"fcstd": "FCStd", "step": "step", "stp": "stp",
           "brep": "brep", "iges": "iges", "igs": "igs", "stl": "stl"}[ext]
    candidates = [
        FREECAD_ASSETS_DIR / f"{base}.{ext}",
        FREECAD_ASSETS_DIR / f"{base.capitalize()}.{ext}",
    ]
    # Try case-insensitive lookup against the actual dir contents.
    if FREECAD_ASSETS_DIR.exists():
        lower_map = {p.name.lower(): p for p in FREECAD_ASSETS_DIR.iterdir()}
        for cand in (*candidates,):
            hit = lower_map.get(cand.name.lower())
            if hit:
                return hit
        # Hyphen-stem fallback: the generator saves with sanitised filenames
        # that may have hyphens in different places than the slug.
        slug = base.replace("_", "").replace("-", "").lower()
        for p in FREECAD_ASSETS_DIR.iterdir():
            stem = p.stem.replace("_", "").replace("-", "").lower()
            if stem == slug and p.suffix.lower().lstrip(".") == ext.lower():
                return p
    return None


def resolve_blender_goal_asset(goal_png: Path) -> Path | None:
    """Map e.g. `A_02_loaded_01_cube.png` -> `01_cube.blend`."""
    hit = _asset_from_sidecar(goal_png)
    if hit:
        return hit
    name = goal_png.name
    m = re.match(r"^[A-Z]_\d+_loaded_(\d+_[a-z_0-9]+)\.png$", name, re.IGNORECASE)
    if not m:
        return None
    base = m.group(1)
    cand = BLENDER_ASSETS_DIR / f"{base}.blend"
    return cand if cand.exists() else None


# --- code extraction -------------------------------------------------------

def extract_python_chunks(trajectory_json: dict, app: str) -> list[str]:
    """Return the agent's executable Python chunks in order.

    For FreeCAD: every `type` action whose text looks like Python.
    For Blender: every `python_eval` code field, plus `type` actions
                 that look like Python.
    """
    chunks: list[str] = []
    for step in trajectory_json.get("trajectory", []):
        a = step.get("action") or {}
        t = a.get("type")
        if t == "python_eval":
            code = a.get("code", "")
            if isinstance(code, str) and code.strip():
                chunks.append(code)
        elif t == "type":
            text = a.get("text", "")
            if isinstance(text, str) and _looks_pythonic(text):
                chunks.append(text)
    return chunks


_PY_HINTS = (
    "import ", "from ", "doc ", "doc=",
    "App.", "Gui.", "Part.", "bpy.", "App=", "obj ", "obj=",
    "b ", "b=", "v=", "v ", "o=", "o ",
    "shape", "[", "for ", "if ", "with ",
)


def _looks_pythonic(text: str) -> bool:
    s = text.lstrip()
    return any(s.startswith(h) for h in _PY_HINTS) or "=" in s


# --- metric dataclass -------------------------------------------------------

@dataclass
class GeometryStats:
    found: bool = False
    object_count: int = 0
    object_names: list[str] = field(default_factory=list)
    volume: float = 0.0
    surface_area: float = 0.0
    bbox: tuple[float, float, float] = (0.0, 0.0, 0.0)
    face_count: int = 0
    edge_count: int = 0
    vertex_count: int = 0
    error: str | None = None


@dataclass
class MatchScore:
    obj_present: bool
    name_overlap: float       # 0..1: jaccard of object name sets
    vol_ratio: float           # 0..1: min/max of volumes (SCALE)
    bbox_score: float          # 0..1: mean per-axis ratio (SCALE)
    shape_proportions: float   # 0..1: per-axis ratio on NORMALIZED bboxes (SHAPE — scale-independent)
    face_ratio: float          # 0..1: min/max of face counts (TOPOLOGY)
    vert_ratio: float          # 0..1: min/max of vertex counts (TOPOLOGY)
    match_score: float         # 0..100 weighted composite


# --- comparison ------------------------------------------------------------
#
# Reward design (v2): shape > scale.
# Per user requirement: "concerned first with the ability to recreate the
# shape of the asset and then after that the correct scale."
#
#                                  v1 (prior)   v2 (current)   bucket
#   obj_present                       15            15         building
#   name_overlap                      10            10         naming
#   vol_ratio (scale)                 25            10         SCALE   ↓ 15
#   bbox_score (absolute size)        25             5         SCALE   ↓ 20
#   shape_proportions (NEW)            -            25         SHAPE   +25
#   face_ratio (topology)             15            20         SHAPE   ↑ 5
#   vert_ratio (topology)             10            15         SHAPE   ↑ 5
#                                    ---           ---
#                                    100           100
#
# Shape-related total: 60 pts (shape_proportions + face + vert)
# Scale-related total: 15 pts (vol_ratio + bbox_score)
# Building/naming:     25 pts (obj_present + name_overlap)
#
# shape_proportions is the new scale-independent component: it normalizes
# each bbox to its longest axis (so a 1x1x4 rod matches another 1x1x4 rod
# whether the absolute scale is mm or km), then compares per-axis ratios.
# This captures "is the shape a slab / cube / rod / disk?" independent of
# whether the agent's dimensions are 10× too small.

def compare(goal: GeometryStats, agent: GeometryStats,
            weights: dict | None = None) -> MatchScore:
    weights = weights or {
        "obj_present":       15,
        "name_overlap":      10,
        "vol_ratio":         10,   # ↓ from 25 (scale demoted)
        "bbox_score":         5,   # ↓ from 25 (scale demoted; mostly absorbed by shape_proportions)
        "shape_proportions": 25,   # NEW: normalized bbox per-axis ratio
        "face_ratio":        20,   # ↑ from 15 (topology promoted)
        "vert_ratio":        15,   # ↑ from 10 (topology promoted)
    }

    def ratio(a: float, b: float) -> float:
        if a <= 0 or b <= 0:
            return 0.0
        return min(a, b) / max(a, b)

    def normalize_bbox(bb: tuple[float, float, float]) -> tuple[float, float, float]:
        m = max(bb) if max(bb) > 0 else 1.0
        return (bb[0]/m, bb[1]/m, bb[2]/m)

    obj_present = agent.found and agent.object_count >= 1
    name_overlap = (
        len(set(map(str.lower, goal.object_names)) & set(map(str.lower, agent.object_names)))
        / max(1, len(set(map(str.lower, goal.object_names))))
        if goal.object_names else 0.0
    )
    vol_ratio = ratio(goal.volume, agent.volume)
    bbox_score = sum(ratio(g, a) for g, a in zip(goal.bbox, agent.bbox)) / 3
    # NEW: normalize each bbox to its longest axis, then compute per-axis ratio.
    # Captures shape character (slab vs cube vs rod) independent of absolute scale.
    g_norm = normalize_bbox(goal.bbox)
    a_norm = normalize_bbox(agent.bbox)
    shape_proportions = sum(ratio(g, a) for g, a in zip(g_norm, a_norm)) / 3
    face_ratio = ratio(goal.face_count, agent.face_count)
    vert_ratio = ratio(goal.vertex_count, agent.vertex_count)

    score = (
        (weights["obj_present"] if obj_present else 0)
        + weights["name_overlap"] * name_overlap
        + weights["vol_ratio"] * vol_ratio
        + weights["bbox_score"] * bbox_score
        + weights["shape_proportions"] * shape_proportions
        + weights["face_ratio"] * face_ratio
        + weights["vert_ratio"] * vert_ratio
    )
    return MatchScore(
        obj_present=obj_present,
        name_overlap=round(name_overlap, 3),
        vol_ratio=round(vol_ratio, 3),
        bbox_score=round(bbox_score, 3),
        shape_proportions=round(shape_proportions, 3),
        face_ratio=round(face_ratio, 3),
        vert_ratio=round(vert_ratio, 3),
        match_score=round(score, 1),
    )


# --- subprocess wrappers ---------------------------------------------------

def _run_freecadcmd(script: Path, env_extra: dict, timeout: float = 240) -> tuple[int, str, str]:
    # freecadcmd swallows positional args as files-to-open; pass args via env.
    import os
    env = {**os.environ, **env_extra}
    res = subprocess.run(
        ["freecadcmd", str(script)],
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    return res.returncode, res.stdout, res.stderr


def _run_blender_headless(script: Path, *args: str, timeout: float = 240) -> tuple[int, str, str]:
    cmd = ["blender", "-b", "-P", str(script)]
    if args:
        cmd.append("--")
        cmd.extend(args)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                         env={**__import__("os").environ,
                              "LIBGL_ALWAYS_SOFTWARE": "1"})
    return res.returncode, res.stdout, res.stderr


# --- per-app entry points --------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = REPO_ROOT / "scripts"


def evaluate_freecad_run(trajectory_json: Path,
                         goal_asset: Path,
                         work_dir: Path) -> dict[str, Any]:
    """Reconstruct + measure for a FreeCAD trajectory."""
    work_dir.mkdir(parents=True, exist_ok=True)
    traj = json.loads(trajectory_json.read_text())
    chunks = extract_python_chunks(traj, "freecad")
    chunks_path = work_dir / "agent_chunks.json"
    chunks_path.write_text(json.dumps(chunks))
    agent_fcstd = work_dir / "agent_model.FCStd"
    agent_stats_path = work_dir / "agent_stats.json"
    rc1, so1, se1 = _run_freecadcmd(
        _SCRIPTS / "freecad_reconstruct.py",
        {"FCRECON_CHUNKS": str(chunks_path),
         "FCRECON_OUT": str(agent_fcstd),
         "FCRECON_STATS": str(agent_stats_path)},
    )
    agent_stats = _load_stats(agent_stats_path, fallback_error=(se1 or so1)[-400:])

    goal_stats_path = work_dir / "goal_stats.json"
    rc2, so2, se2 = _run_freecadcmd(
        _SCRIPTS / "freecad_measure.py",
        {"FCMEAS_ASSET": str(goal_asset),
         "FCMEAS_STATS": str(goal_stats_path)},
    )
    goal_stats = _load_stats(goal_stats_path, fallback_error=(se2 or so2)[-400:])

    return {
        "app": "freecad",
        "goal_asset": str(goal_asset),
        "agent_model": str(agent_fcstd) if agent_fcstd.exists() else None,
        "chunks_count": len(chunks),
        "agent_stats": asdict(agent_stats),
        "goal_stats": asdict(goal_stats),
        "score": asdict(compare(goal_stats, agent_stats)),
        "reconstruct_rc": rc1,
        "measure_rc": rc2,
    }


def evaluate_blender_run(trajectory_json: Path,
                         goal_asset: Path,
                         work_dir: Path) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    traj = json.loads(trajectory_json.read_text())
    chunks = extract_python_chunks(traj, "blender")
    chunks_path = work_dir / "agent_chunks.json"
    chunks_path.write_text(json.dumps(chunks))
    agent_blend = work_dir / "agent_model.blend"
    agent_stats_path = work_dir / "agent_stats.json"
    rc1, so1, se1 = _run_blender_headless(
        _SCRIPTS / "blender_reconstruct.py",
        str(chunks_path), str(agent_blend), str(agent_stats_path),
    )
    agent_stats = _load_stats(agent_stats_path, fallback_error=(se1 or so1)[-400:])

    goal_stats_path = work_dir / "goal_stats.json"
    rc2, so2, se2 = _run_blender_headless(
        _SCRIPTS / "blender_measure.py",
        str(goal_asset), str(goal_stats_path),
    )
    goal_stats = _load_stats(goal_stats_path, fallback_error=(se2 or so2)[-400:])

    return {
        "app": "blender",
        "goal_asset": str(goal_asset),
        "agent_model": str(agent_blend) if agent_blend.exists() else None,
        "chunks_count": len(chunks),
        "agent_stats": asdict(agent_stats),
        "goal_stats": asdict(goal_stats),
        "score": asdict(compare(goal_stats, agent_stats)),
        "reconstruct_rc": rc1,
        "measure_rc": rc2,
    }


def _load_stats(path: Path, fallback_error: str) -> GeometryStats:
    if not path.exists():
        return GeometryStats(error=f"stats file not written: {fallback_error[:200]}")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return GeometryStats(error=f"stats JSON invalid: {exc}")
    return GeometryStats(
        found=raw.get("found", False),
        object_count=raw.get("object_count", 0),
        object_names=raw.get("object_names", []),
        volume=raw.get("volume", 0.0),
        surface_area=raw.get("surface_area", 0.0),
        bbox=tuple(raw.get("bbox", (0.0, 0.0, 0.0))),
        face_count=raw.get("face_count", 0),
        edge_count=raw.get("edge_count", 0),
        vertex_count=raw.get("vertex_count", 0),
        error=raw.get("error"),
    )
