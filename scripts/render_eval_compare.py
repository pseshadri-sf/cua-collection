"""Render goal vs agent-reconstruction side-by-side for manual review.

For each job in a parallel-orchestrator run, produces a composite PNG:

  +------------------+------------------+
  | GOAL (cropped)   | AGENT FINAL VIEW |
  +------------------+------------------+
  | Job ID | match_score | shape | bbox | vol | name

Sources:
  goal           = job_dir/goal.png  (or job_dir/eval/eval.json's goal_asset
                                       resolved + rendered fresh if missing)
  agent_final    = last `frames/step_NN_before.png` taken before the agent
                   terminated — shows what was actually in the viewport
  scores         = job_dir/eval/eval.json

Output: <run_dir>/compare/<rank>_<score>_<job_id>.png (sorted by score, desc)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


PANEL_W, PANEL_H = 640, 480
BANNER_H = 120
PAD = 12
PART_W, PART_H = 240, 180
PARTS_BANNER_H = 28
BG = (28, 28, 32)
FG = (235, 235, 240)
FG_DIM = (170, 170, 180)
GOOD = (108, 220, 140)
WARN = (230, 175, 80)
BAD  = (235, 110, 110)


def _fit(img: Image.Image, w: int, h: int) -> Image.Image:
    out = Image.new("RGB", (w, h), (12, 12, 14))
    im2 = img.copy()
    im2.thumbnail((w - 8, h - 8), Image.LANCZOS)
    x = (w - im2.width) // 2
    y = (h - im2.height) // 2
    out.paste(im2, (x, y))
    return out


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _score_color(s: float) -> tuple[int, int, int]:
    if s >= 75: return GOOD
    if s >= 45: return WARN
    return BAD


def _last_frame(job_dir: Path) -> Path | None:
    frames = sorted((job_dir / "frames").glob("step_*_before.png"))
    return frames[-1] if frames else None


def _parts_for_job(job_dir: Path) -> list[dict] | None:
    """Return the parts list from the goal sidecar (or None)."""
    # Find the goal_path from eval.json → derive sidecar path
    ej = job_dir / "eval" / "eval.json"
    if not ej.exists():
        return None
    try:
        e = json.loads(ej.read_text())
    except json.JSONDecodeError:
        return None
    # The goal_asset is the source file path; we need to find the sidecar that
    # corresponds to the goal_PNG used in the trajectory. We have the goal.png
    # copy in job_dir, but we need to find the original to locate the sidecar.
    # Easiest: scan E_*_loaded_<stem>.meta.json files that match the goal_asset.
    asset_stem = Path(e.get("goal_asset", "")).stem
    if not asset_stem:
        return None
    import glob
    # Try BL first
    for root in ("/home/ubuntu/cua_blender_smoketest/screenshots",
                 "/home/ubuntu/cua_gui_smoketest/screenshots"):
        for sc in glob.glob(f"{root}/E_*loaded_*.meta.json"):
            try:
                d = json.loads(Path(sc).read_text())
            except Exception: continue
            if Path(d.get("asset","")).stem == asset_stem:
                return d.get("parts")
    return None


def _agent_view(job_dir: Path) -> tuple[Path, str] | None:
    """Prefer the clean headless re-render; fall back to last trajectory frame.
    Returns (path, label) where label is what to write above the panel.
    """
    render = job_dir / "eval" / "agent_render.png"
    if render.exists() and render.stat().st_size > 0:
        return (render, "AGENT (clean re-render of reconstructed model)")
    last = _last_frame(job_dir)
    if last is not None:
        return (last, "AGENT (last viewport frame — may be unframed)")
    return None


def build_one(job_dir: Path) -> Image.Image | None:
    goal = job_dir / "goal.png"
    if not goal.exists():
        return None
    ej = job_dir / "eval" / "eval.json"
    if not ej.exists():
        return None
    e = json.loads(ej.read_text())
    score = e.get("score", {})
    match = float(score.get("match_score", 0))

    av = _agent_view(job_dir)
    if av is None:
        return None
    agent_path, agent_label = av

    # Optional sub-object strip: load parts from sidecar + their thumbnails
    parts = _parts_for_job(job_dir)
    parts_with_thumbs: list[tuple[dict, Path | None]] = []
    if parts:
        asset_stem = Path(json.loads((job_dir / "eval" / "eval.json").read_text())
                          .get("goal_asset","")).stem
        cache = Path("/tmp/decomposed_parts_cache") / asset_stem
        for p in parts[:8]:    # cap at 8 sub-parts to keep the image manageable
            idx = p.get("index", 0)
            thumb = cache / f"part_{idx:02d}.png"
            parts_with_thumbs.append((p, thumb if thumb.exists() else None))

    W = PANEL_W * 2 + PAD * 3
    parts_h = (PART_H + PARTS_BANNER_H + PAD) if parts_with_thumbs else 0
    H = PANEL_H + BANNER_H + PAD * 2 + parts_h
    canvas = Image.new("RGB", (W, H), BG)

    goal_im  = _fit(Image.open(goal),       PANEL_W, PANEL_H)
    agent_im = _fit(Image.open(agent_path), PANEL_W, PANEL_H)
    canvas.paste(goal_im,  (PAD, PAD))
    canvas.paste(agent_im, (PAD * 2 + PANEL_W, PAD))

    draw = ImageDraw.Draw(canvas)
    sub  = _font(18)
    draw.text((PAD + 6, PAD + 6), "GOAL", fill=FG, font=sub)
    draw.text((PAD * 2 + PANEL_W + 6, PAD + 6), agent_label, fill=FG, font=sub)

    y0 = PAD + PANEL_H + PAD
    big   = _font(32)
    med   = _font(18)
    small = _font(14)
    draw.text((PAD, y0), f"match_score = {match:.1f}", fill=_score_color(match), font=big)

    comp_y = y0 + 44
    comps = [
        ("obj_present",       score.get("obj_present")),
        ("name_overlap",      score.get("name_overlap")),
        ("vol_ratio",         score.get("vol_ratio")),
        ("bbox_score",        score.get("bbox_score")),
        ("shape_proportions", score.get("shape_proportions")),
        ("face_ratio",        score.get("face_ratio")),
        ("vert_ratio",        score.get("vert_ratio")),
    ]
    x = PAD
    for name, val in comps:
        v = "—" if val is None else (f"{val:.2f}" if isinstance(val, float)
                                     else str(val))
        chunk = f"{name}: {v}"
        draw.text((x, comp_y), chunk, fill=FG_DIM, font=small)
        x += int(med.getlength(chunk) * 0.95) + 24

    info_y = comp_y + 22
    job = job_dir.name
    goal_asset = Path(e.get("goal_asset", "")).name
    info = f"{job}    goal_asset={goal_asset}    chunks={e.get('chunks_count', '?')}"
    draw.text((PAD, info_y), info, fill=FG_DIM, font=small)

    # agent vs goal stats line
    gs = e.get("goal_stats")  or {}
    as_ = e.get("agent_stats") or {}
    stats_y = info_y + 20
    def _fmt_bbox(s):
        bb = s.get("bbox") or [0, 0, 0]
        return f"[{bb[0]:.1f},{bb[1]:.1f},{bb[2]:.1f}]"
    line = (f"goal: bbox={_fmt_bbox(gs)} faces={gs.get('face_count','?')} "
            f"vol={gs.get('volume',0):.0f}    "
            f"agent: bbox={_fmt_bbox(as_)} faces={as_.get('face_count','?')} "
            f"vol={as_.get('volume',0):.0f}")
    draw.text((PAD, stats_y), line, fill=FG_DIM, font=small)

    # ── Per-part sub-object strip ──────────────────────────────────────────
    if parts_with_thumbs:
        strip_y = PAD + PANEL_H + BANNER_H + PAD
        # Banner label
        n_parts_total = len(parts) if parts else 0
        label = f"GOAL SUB-PARTS (showing {len(parts_with_thumbs)} of {n_parts_total} — ordered by volume desc)"
        draw.text((PAD, strip_y), label, fill=FG, font=sub)
        strip_y += PARTS_BANNER_H
        # Lay out N thumbnails left-to-right; if too many, scale down width
        avail_w = W - PAD * 2
        slot_w = min(PART_W, avail_w // max(1, len(parts_with_thumbs)))
        for i, (p, thumb) in enumerate(parts_with_thumbs):
            x = PAD + i * slot_w
            cell = Image.new("RGB", (slot_w, PART_H), (12, 12, 14))
            if thumb is not None and thumb.exists():
                try:
                    im = Image.open(thumb)
                    im.thumbnail((slot_w - 4, PART_H - 30), Image.LANCZOS)
                    cell.paste(im, ((slot_w - im.width) // 2, 2))
                except Exception: pass
            # Label
            cdraw = ImageDraw.Draw(cell)
            bbox = p.get("bbox") or [0, 0, 0]
            name = (p.get("name") or f"part_{p.get('index',0):02d}")[:18]
            cdraw.text((4, PART_H - 28), name, fill=FG, font=small)
            cdraw.text((4, PART_H - 14),
                       f"[{bbox[0]:.1f}, {bbox[1]:.1f}, {bbox[2]:.1f}]",
                       fill=FG_DIM, font=small)
            canvas.paste(cell, (x, strip_y))

    return canvas


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run_dir", type=Path)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--top", type=int, default=0,
                   help="Also build a grid of the top N highest-scoring jobs.")
    args = p.parse_args(argv)

    out = args.out or args.run_dir / "compare"
    out.mkdir(parents=True, exist_ok=True)

    # Discover all jobs, dedupe by job_id keeping best-score copy
    candidates: dict[str, tuple[float, Path, Image.Image]] = {}
    for job_dir in args.run_dir.glob("worker_*/jobs/*"):
        if not job_dir.is_dir(): continue
        ej = job_dir / "eval" / "eval.json"
        if not ej.exists(): continue
        try:
            score = float(json.loads(ej.read_text()).get("score", {}).get("match_score", 0))
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if job_dir.name in candidates and candidates[job_dir.name][0] >= score:
            continue
        img = build_one(job_dir)
        if img is None: continue
        candidates[job_dir.name] = (score, job_dir, img)

    if not candidates:
        print("no jobs found", file=sys.stderr); return 1

    ranked = sorted(candidates.items(), key=lambda kv: -kv[1][0])
    paths: list[Path] = []
    for rank, (jid, (score, jd, img)) in enumerate(ranked, 1):
        path = out / f"{rank:02d}_score{int(round(score)):03d}_{jid}.png"
        img.save(path, "PNG", optimize=True)
        paths.append(path)
        print(f"  [{score:5.1f}]  {path.name}")

    print(f"\nwrote {len(paths)} comparisons to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
