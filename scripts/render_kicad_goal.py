"""Render a multi-view goal atlas for a KiCad board (mirrors render_goal_multiview).

For each goal `.kicad_pcb`, produces a single PNG atlas with 4 tiles:

    +-----------+-----------+
    |  TOP 2D   | BOTTOM 2D |
    +-----------+-----------+
    | SILK/EDGE |   3D TOP  |
    +-----------+-----------+

2D layer plots come from `kicad-cli pcb export svg` (tight to the board via
--exclude-drawing-sheet --fit-page-to-board) rasterised with rsvg-convert; the
3D tile from `kicad-cli pcb render`. Tiles are composed + labelled with PIL.

Also writes the goal metadata sidecar `<out>.meta.json` (asset + the kicad{}
grounding block) by invoking extract_goal_metadata.py under the system python3
that has pcbnew — so the goal is self-contained (image + grounding) and the
orchestrator's grounding step is a no-op.

Runs under the project venv (PIL); shells out to kicad-cli / rsvg-convert /
the system python3. Cached per stem under /tmp/kicad_goal_cache/<stem>.png.

Usage:
    uv run python scripts/render_kicad_goal.py --asset <board.kicad_pcb> [--out <png>] [--force]
    uv run python scripts/render_kicad_goal.py --jobs-file <jobs.jsonl> [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_EXTRACTOR = _REPO / "scripts" / "extract_goal_metadata.py"
_SYS_PY = "/usr/bin/python3"
_CACHE = Path("/tmp/kicad_goal_cache")

# (label, kicad-cli layer list, mirror?) per 2D tile.
_VIEWS_2D = [
    ("TOP (F.Cu/silk/edge)", "F.Cu,F.Silkscreen,Edge.Cuts", False),
    ("BOTTOM (B.Cu/silk/edge)", "B.Cu,B.Silkscreen,Edge.Cuts", True),
    ("SILK + EDGE", "F.Silkscreen,Edge.Cuts", False),
]
_TILE = (600, 450)


def _kicad_cli(*args: str, timeout: float = 120.0) -> bool:
    try:
        r = subprocess.run(["kicad-cli", *args], capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode == 0
    except subprocess.SubprocessError:
        return False


def _svg_to_png(svg: Path, png: Path, w: int) -> bool:
    if not shutil.which("rsvg-convert") or not svg.exists():
        return False
    try:
        r = subprocess.run(["rsvg-convert", "-w", str(w), "-b", "white",
                            "-o", str(png), str(svg)], capture_output=True, timeout=60)
        return r.returncode == 0 and png.exists() and png.stat().st_size > 0
    except subprocess.SubprocessError:
        return False


def _render_tile_2d(board: Path, layers: str, mirror: bool, work: Path, idx: int) -> "Path | None":
    svg = work / f"v{idx}.svg"
    args = ["pcb", "export", "svg", "--layers", layers, "--exclude-drawing-sheet",
            "--fit-page-to-board", "--page-size-mode", "2", "-o", str(svg)]
    if mirror:
        args.append("--mirror")
    args.append(str(board))
    if not _kicad_cli(*args):
        return None
    png = work / f"v{idx}.png"
    return png if _svg_to_png(svg, png, _TILE[0]) else None


def _render_tile_3d(board: Path, work: Path) -> "Path | None":
    png = work / "v3.png"
    if _kicad_cli("pcb", "render", "--side", "top",
                  "--width", str(_TILE[0]), "--height", str(_TILE[1]),
                  "-o", str(png), str(board), timeout=180) and png.exists():
        return png
    return None


def _compose(tiles: list[tuple[str, "Path | None"]], out: Path) -> bool:
    from PIL import Image, ImageDraw, ImageFont  # local import (venv)
    tw, th = _TILE
    pad, label_h = 8, 22
    cols, rows = 2, 2
    W = cols * tw + (cols + 1) * pad
    H = rows * (th + label_h) + (rows + 1) * pad
    atlas = Image.new("RGB", (W, H), (30, 30, 35))
    draw = ImageDraw.Draw(atlas)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 15)
    except OSError:
        font = ImageFont.load_default()
    for i, (label, tile) in enumerate(tiles[:4]):
        r, c = divmod(i, cols)
        x = pad + c * (tw + pad)
        y = pad + r * (th + label_h + pad)
        if tile and tile.exists():
            try:
                im = Image.open(tile).convert("RGB")
                im.thumbnail((tw, th))
                cell = Image.new("RGB", (tw, th), (245, 245, 245))
                cell.paste(im, ((tw - im.width) // 2, (th - im.height) // 2))
                atlas.paste(cell, (x, y))
            except Exception:  # noqa: BLE001
                draw.rectangle([x, y, x + tw, y + th], fill=(60, 60, 60))
        else:
            draw.rectangle([x, y, x + tw, y + th], fill=(60, 60, 60))
            draw.text((x + 10, y + th // 2), "(unavailable)", fill=(200, 200, 200), font=font)
        draw.text((x + 4, y + th + 3), label, fill=(230, 230, 240), font=font)
    out.parent.mkdir(parents=True, exist_ok=True)
    atlas.save(out)
    return True


def _write_sidecar(board: Path, out_png: Path) -> bool:
    """Write <out_png>.meta.json grounding sidecar via the system-python pcbnew
    extractor (asset + kicad{} block), so the goal is self-contained."""
    sidecar = out_png.with_suffix(".meta.json")
    env = {**os.environ, "META_ASSET": str(board), "META_OUT": str(sidecar),
           "META_APP": "kicad"}
    try:
        r = subprocess.run([_SYS_PY, str(_EXTRACTOR)], env=env,
                           capture_output=True, timeout=120)
        return r.returncode == 0 and sidecar.exists()
    except subprocess.SubprocessError:
        return False


def render_goal(board: Path, out_png: Path | None = None, force: bool = False) -> Path | None:
    board = Path(board)
    if out_png is None:
        _CACHE.mkdir(parents=True, exist_ok=True)
        out_png = _CACHE / f"{board.stem}.png"
    if out_png.exists() and not force:
        return out_png
    work = Path(tempfile.mkdtemp(prefix="kigoal_"))
    try:
        tiles: list[tuple[str, Path | None]] = []
        for i, (label, layers, mirror) in enumerate(_VIEWS_2D):
            tiles.append((label, _render_tile_2d(board, layers, mirror, work, i)))
        tiles.append(("3D TOP", _render_tile_3d(board, work)))
        if not any(t for _, t in tiles):
            print(f"[render] no tiles produced for {board.name}", file=sys.stderr)
            return None
        _compose(tiles, out_png)
        _write_sidecar(board, out_png)
        print(f"[render] {out_png}  ({sum(1 for _, t in tiles if t)}/4 tiles)")
        return out_png
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--asset", type=Path, help="A .kicad_pcb goal board")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--jobs-file", type=Path, default=None,
                   help="JSONL with asset_path/goal_path per line (batch mode)")
    p.add_argument("--force", action="store_true")
    args = p.parse_args(argv)

    if args.asset:
        out = render_goal(args.asset, args.out, args.force)
        return 0 if out else 1
    if args.jobs_file:
        n_ok = 0
        for line in args.jobs_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            j = json.loads(line)
            board = j.get("asset_path") or j.get("asset")
            if board and Path(board).exists():
                if render_goal(Path(board), None, args.force):
                    n_ok += 1
        print(f"[render] {n_ok} goal atlases")
        return 0
    p.error("one of --asset or --jobs-file is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
