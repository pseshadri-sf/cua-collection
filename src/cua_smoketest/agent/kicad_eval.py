"""KiCad PCB-reconstruction metric (no Chamfer/volume analogue exists).

compare_kicad scores an agent's reconstructed board against the goal board on a
0-100 composite (SPEC-kicad-pipeline.md §8):

  footprint placement   35   matched footprints (ref/value) within pos+rot tol
  connectivity / nets    25   net-set Jaccard + net-count agreement
  board outline          15   outline bbox shape match (w/h proportions)
  count agreement        10   footprint / net / layer count ratios
  layer-image similarity 15   OPTIONAL (SSIM of rendered plots) — when not
                              computed, its weight is renormalized away.

evaluate_kicad_run mirrors evaluate_freecad_run: replay the agent's pcbnew_eval
chunks onto the blank seed board (kicad_reconstruct.py) -> agent_stats; measure
the goal board (kicad_measure.py) -> goal_stats; compare. Both headless scripts
run under the system python3 that carries the pcbnew module.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO / "scripts"
_SEED_BOARD = _REPO / "assets" / "kicad" / "_blank.kicad_pcb"
_SYS_PY = "/usr/bin/python3"  # the interpreter that has the pcbnew module

# Tolerances for footprint placement scoring.
_POS_TOL_MM = 5.0    # full credit within this; linear falloff to 0 at 3x
_ROT_TOL_DEG = 15.0

# Component weights (layer_image optional; renormalized if absent).
_WEIGHTS = {
    "footprint_placement": 35,
    "connectivity_nets": 25,
    "outline_match": 15,
    "count_agreement": 10,
    "layer_image": 15,
}


@dataclass
class KiCadScore:
    footprint_placement: float = 0.0   # 0..1
    connectivity_nets: float = 0.0     # 0..1
    outline_match: float = 0.0         # 0..1
    count_agreement: float = 0.0       # 0..1
    layer_image: float | None = None   # 0..1 or None (not computed)
    match_score: float = 0.0           # 0..100 weighted composite
    matched_footprints: int = 0
    goal_footprints: int = 0
    agent_footprints: int = 0
    notes: list[str] = field(default_factory=list)


def _ratio(a: float, b: float) -> float:
    """Symmetric size ratio in [0,1]: min/max, 1.0 when both ~0."""
    a, b = abs(a), abs(b)
    if a == 0 and b == 0:
        return 1.0
    if a == 0 or b == 0:
        return 0.0
    return min(a, b) / max(a, b)


def _match_footprints(goal: list[dict], agent: list[dict]) -> list[tuple[dict, dict]]:
    """Greedy match: exact ref first, then by footprint value/name, then by
    nearest free position. Refs are usually unique + identical (R1<->R1)."""
    pairs: list[tuple[dict, dict]] = []
    used_a = set()
    by_ref_a = {a["ref"]: i for i, a in enumerate(agent)}
    # 1) exact ref
    remaining_g = []
    for g in goal:
        j = by_ref_a.get(g["ref"])
        if j is not None and j not in used_a:
            pairs.append((g, agent[j])); used_a.add(j)
        else:
            remaining_g.append(g)
    # 2) by name/value then nearest position among unused agents
    for g in remaining_g:
        best = None; best_d = 1e18
        for j, a in enumerate(agent):
            if j in used_a:
                continue
            name_ok = (a.get("name", "").split(":")[-1] == g.get("name", "").split(":")[-1])
            dx = a["at"][0] - g["at"][0]; dy = a["at"][1] - g["at"][1]
            d = (dx * dx + dy * dy) ** 0.5
            score_d = d if name_ok else d + 1000.0  # prefer same-value matches
            if score_d < best_d:
                best_d = score_d; best = j
        if best is not None:
            pairs.append((g, agent[best])); used_a.add(best)
    return pairs


def _placement_score(goal: list[dict], agent: list[dict]) -> tuple[float, int]:
    if not goal:
        return (1.0 if not agent else 0.0), 0
    pairs = _match_footprints(goal, agent)
    total = 0.0
    for g, a in pairs:
        dx = a["at"][0] - g["at"][0]; dy = a["at"][1] - g["at"][1]
        dist = (dx * dx + dy * dy) ** 0.5
        pos = max(0.0, 1.0 - dist / (3.0 * _POS_TOL_MM))
        drot = abs((a.get("rot", 0) - g.get("rot", 0)) % 360.0)
        drot = min(drot, 360.0 - drot)
        rot = 1.0 if drot <= _ROT_TOL_DEG else max(0.0, 1.0 - (drot - _ROT_TOL_DEG) / 90.0)
        total += 0.75 * pos + 0.25 * rot
    # denominator = max(goal, agent) so missing AND extra footprints are penalized
    denom = max(len(goal), len(agent))
    return total / denom, len(pairs)


def _nets_score(goal: dict, agent: dict) -> float:
    gn = set(goal.get("nets") or []); an = set(agent.get("nets") or [])
    if not gn and not an:
        # no named nets either side — fall back to net-count agreement
        return _ratio(goal.get("net_count", 0), agent.get("net_count", 0))
    union = gn | an
    jacc = (len(gn & an) / len(union)) if union else 1.0
    return 0.6 * jacc + 0.4 * _ratio(goal.get("net_count", 0), agent.get("net_count", 0))


def _outline_score(goal: dict, agent: dict) -> float:
    gw, gh = (goal.get("outline_bbox_mm") or [0, 0])[:2]
    aw, ah = (agent.get("outline_bbox_mm") or [0, 0])[:2]
    return 0.5 * _ratio(gw, aw) + 0.5 * _ratio(gh, ah)


def _count_score(goal: dict, agent: dict) -> float:
    return (
        _ratio(goal.get("footprint_count", 0), agent.get("footprint_count", 0))
        + _ratio(goal.get("net_count", 0), agent.get("net_count", 0))
        + _ratio(goal.get("layer_count", 0), agent.get("layer_count", 0))
    ) / 3.0


def compare_kicad(goal_stats: dict, agent_stats: dict,
                  layer_image: float | None = None) -> KiCadScore:
    """Composite PCB-reconstruction score. `layer_image` (0..1) is optional; when
    None its weight is renormalized across the other components."""
    place, matched = _placement_score(goal_stats.get("footprints") or [],
                                      agent_stats.get("footprints") or [])
    nets = _nets_score(goal_stats, agent_stats)
    outline = _outline_score(goal_stats, agent_stats)
    counts = _count_score(goal_stats, agent_stats)

    comps = {
        "footprint_placement": place,
        "connectivity_nets": nets,
        "outline_match": outline,
        "count_agreement": counts,
    }
    if layer_image is not None:
        comps["layer_image"] = layer_image
    total_w = sum(_WEIGHTS[k] for k in comps)
    score = sum(_WEIGHTS[k] * v for k, v in comps.items()) / total_w * 100.0 if total_w else 0.0

    notes = []
    if layer_image is None:
        notes.append("layer_image not computed; weight renormalized")
    if not agent_stats.get("found"):
        notes.append("agent board empty")
    return KiCadScore(
        footprint_placement=round(place, 3),
        connectivity_nets=round(nets, 3),
        outline_match=round(outline, 3),
        count_agreement=round(counts, 3),
        layer_image=(round(layer_image, 3) if layer_image is not None else None),
        match_score=round(score, 1),
        matched_footprints=matched,
        goal_footprints=len(goal_stats.get("footprints") or []),
        agent_footprints=len(agent_stats.get("footprints") or []),
        notes=notes,
    )


def _run_sys_py(script: Path, env_extra: dict) -> tuple[int, str, str]:
    env = {**os.environ, **env_extra}
    try:
        r = subprocess.run([_SYS_PY, str(script)], env=env, capture_output=True,
                           text=True, timeout=180)
        return r.returncode, r.stdout, r.stderr
    except subprocess.SubprocessError as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def _load_stats(path: Path, fallback_error: str) -> dict:
    if not path.exists():
        return {"found": False, "footprint_count": 0, "footprints": [], "net_count": 0,
                "nets": [], "layer_count": 0, "track_count": 0, "outline_bbox_mm": [0, 0],
                "pad_count": 0, "error": f"stats not written: {fallback_error[:200]}"}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return {"found": False, "error": f"stats JSON invalid: {exc}"}


def evaluate_kicad_run(trajectory_json: Path, goal_asset: Path,
                       work_dir: Path, seed_board: Path | None = None) -> dict[str, Any]:
    """Reconstruct + measure for a KiCad trajectory. Mirrors evaluate_freecad_run."""
    work_dir.mkdir(parents=True, exist_ok=True)
    # Deferred import to avoid a module-load cycle (evaluator imports this).
    from .evaluator import extract_python_chunks  # noqa: PLC0415
    traj = json.loads(Path(trajectory_json).read_text())
    chunks = extract_python_chunks(traj, "kicad")
    chunks_path = work_dir / "agent_chunks.json"
    chunks_path.write_text(json.dumps(chunks))
    seed = Path(seed_board) if seed_board else _SEED_BOARD
    agent_board = work_dir / "agent_model.kicad_pcb"
    agent_stats_path = work_dir / "agent_stats.json"
    rc1, so1, se1 = _run_sys_py(_SCRIPTS / "kicad_reconstruct.py", {
        "KIRECON_CHUNKS": str(chunks_path), "KIRECON_SEED": str(seed),
        "KIRECON_OUT": str(agent_board), "KIRECON_STATS": str(agent_stats_path)})
    agent_stats = _load_stats(agent_stats_path, (se1 or so1)[-400:])

    goal_stats_path = work_dir / "goal_stats.json"
    rc2, so2, se2 = _run_sys_py(_SCRIPTS / "kicad_measure.py", {
        "KIMEAS_ASSET": str(goal_asset), "KIMEAS_STATS": str(goal_stats_path)})
    goal_stats = _load_stats(goal_stats_path, (se2 or so2)[-400:])

    score = compare_kicad(goal_stats, agent_stats)
    return {
        "app": "kicad",
        "goal_asset": str(goal_asset),
        "agent_model": str(agent_board) if agent_board.exists() else None,
        "chunks_count": len(chunks),
        "agent_stats": agent_stats,
        "goal_stats": goal_stats,
        "score": asdict(score),
        "reconstruct_rc": rc1,
        "measure_rc": rc2,
    }
