"""Rule-based dynamic planner-model routing.

Selects between a cheap default planner and an expensive fallback based on the
goal's metadata sidecar. The rule is app-specific and lives here so both the FC
and KiCad runners can use the same dispatch.

FreeCAD ablation finding (50-asset stratified, 2026-06-11):
  - flash-lite 50.5 mean / pro 53.9 mean — mean lift of +3.4 doesn't justify
    blanket-scaling to pro (32× cost).
  - But pro CRUSHES flash on:
      * `hard` band (face_count 150-500): mean Δ +17.1, pro wins 71%
      * specific failure mode: chains/sprockets/gears where flash hallucinates
        broken full_code -> 0.0 score.
  - Routing pro for those cases recovers ~1000 score-points across the hardest
    ~20% of assets for ~2× cost (still <$1/833 assets).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# Category-name fragments that historically zero out on flash (sprockets, gears,
# threaded fasteners — geometry that depends on mathematical involute / helix
# parameters flash hallucinates).
_FC_HARD_CATEGORY_PATTERNS = (
    "sprocket", "gear", "chain", "thread", "fastener", "screw", "bolt",
    "involute", "helix",
)


def _sidecar_for(goal_png: Path) -> Path | None:
    """Locate the goal-metadata sidecar (`<stem>.meta.json` next to the atlas)."""
    for sc in (Path(str(goal_png)[:-len(goal_png.suffix)] + ".meta.json"),
               goal_png.with_suffix(".meta.json"),
               goal_png.parent / (goal_png.stem + ".meta.json")):
        if sc.exists():
            return sc
    return None


def _load_sidecar(goal_png: Path) -> dict | None:
    sc = _sidecar_for(goal_png)
    if not sc:
        return None
    try:
        return json.loads(sc.read_text())
    except (OSError, ValueError):
        return None


def _category_from_filename(name: str) -> str:
    """The fx2__ / fx__ prefix encodes the FreeCAD-library path. Extract the
    category-ish portion to scan for hard-category patterns."""
    return re.sub(r"[^a-z0-9]+", " ", name.lower())


def _freecad_should_escalate(meta: dict, goal_png: Path) -> tuple[bool, str]:
    """FreeCAD rule (from the 50-asset ablation):
      1. Hard category match (sprocket/gear/chain/thread/fastener/...) — flash
         systematically zeros these out; pro handles the parametric geometry.
      2. face_count in the hard band [150, 500] — pro mean +17 pts there.
    Returns (escalate?, reason)."""
    # 1. Category match (cheap: regex on the filename + sidecar shape descriptor)
    blob = _category_from_filename((meta.get("asset") or "") + " " +
                                   (meta.get("shape_descriptor") or "") + " " +
                                   goal_png.name)
    for pat in _FC_HARD_CATEGORY_PATTERNS:
        if pat in blob:
            return True, f"category:{pat}"
    # 2. face_count band
    fc = meta.get("face_count")
    if isinstance(fc, (int, float)) and 150 <= fc < 500:
        return True, f"face_count:{int(fc)}"
    return False, ""


# KiCad rule placeholder. The 2026-06-10 100-board ablation showed flash ≈ pro
# at 12 boards (flash 27.5 / flash-lite 23.0 / pro 27.5, 9/12 all-tie). The
# planned smoke test will revisit on 50 boards and a stricter hardness signal
# (footprint_count); the rule below is provisional pending that result.
def _kicad_should_escalate(meta: dict, goal_png: Path) -> tuple[bool, str]:
    """KiCad rule (provisional). Defaults to NEVER escalate until ablation
    proves a routing pays off. Wire-in is identical to FC so we can flip the
    rule independently."""
    return False, ""


def select_planner_model(goal_png: Path, default_model: str,
                         escalate_model: str | None, app: str) -> tuple[str, str]:
    """Pick the planner model for this asset. Returns (model, reason)."""
    if not escalate_model or default_model == escalate_model:
        return default_model, "no_fallback"
    meta = _load_sidecar(goal_png)
    if not meta:
        return default_model, "no_sidecar"
    rule = _freecad_should_escalate if app == "freecad" else _kicad_should_escalate
    escalate, reason = rule(meta, goal_png)
    return (escalate_model, f"escalated:{reason}") if escalate else (default_model, "default")
