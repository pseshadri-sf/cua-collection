"""Crop a goal screenshot tightly to the rendered asset using axis projection.

Brightness-threshold + bbox is too sensitive to scattered UI elements (axis
gizmo, navigation cube, tiny labels). Instead, project the foreground mask
onto X and Y axes and crop to the dense regions.

Algorithm:
  1. Crop to viewport bounds (excludes app chrome).
  2. Compute fg mask: pixels above brightness threshold (asset is shaded
     geometry, viewport bg is dark ~50 gray).
  3. Row & column projections: count fg pixels per row, per column.
  4. For each axis, find contiguous run where projection > 5% of peak — that
     is the asset's extent.
  5. Bbox = intersection of x and y dense ranges. Expand by margin, pad to
     full image aspect, crop+upscale to original canvas.
"""
from __future__ import annotations
from pathlib import Path
from PIL import Image

VIEWPORTS = {
    "freecad": (340, 110, 1900, 1040),
    "blender": (60, 50, 1620, 985),
}
BG_THRESHOLD = 90
DENSE_FRAC = 0.05   # column/row counts above 5% of peak count as "asset-dense"


def _dense_range(counts: list[int]) -> tuple[int, int] | None:
    if not counts: return None
    peak = max(counts)
    if peak == 0: return None
    cutoff = max(1, int(peak * DENSE_FRAC))
    # leftmost and rightmost indices where counts >= cutoff
    indices = [i for i, c in enumerate(counts) if c >= cutoff]
    if not indices: return None
    return (indices[0], indices[-1] + 1)


def find_asset_bbox(img: Image.Image, app: str) -> tuple[int,int,int,int] | None:
    vp = VIEWPORTS[app]
    L, T, R, B = vp
    crop = img.crop(vp).convert("L")
    W, H = crop.size
    # Build fg mask as pixel access
    px = crop.load()
    # Compute column sums and row sums of fg pixels (above BG_THRESHOLD)
    col_counts = [0] * W
    row_counts = [0] * H
    for y in range(H):
        for x in range(W):
            if px[x, y] > BG_THRESHOLD:
                col_counts[x] += 1
                row_counts[y] += 1
    xr = _dense_range(col_counts)
    yr = _dense_range(row_counts)
    if xr is None or yr is None:
        return None
    return (xr[0] + L, yr[0] + T, xr[1] + L, yr[1] + T)


def zoom_to_asset(in_path: Path, out_path: Path, app: str,
                  margin_frac: float = 0.10) -> str:
    img = Image.open(in_path).convert("RGB")
    bbox = find_asset_bbox(img, app)
    if bbox is None:
        return "no-asset-found"
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    if w < 50 or h < 50:
        return f"asset-too-small ({w}x{h})"
    mx, my = int(w * margin_frac), int(h * margin_frac)
    x0 = max(0, x0 - mx); y0 = max(0, y0 - my)
    x1 = min(img.width,  x1 + mx); y1 = min(img.height, y1 + my)
    aspect = img.width / img.height
    bw, bh = x1 - x0, y1 - y0
    if bw / bh > aspect:
        target_h = bw / aspect
        extra = (target_h - bh) / 2
        y0 = max(0, int(y0 - extra)); y1 = min(img.height, int(y1 + extra))
    else:
        target_w = bh * aspect
        extra = (target_w - bw) / 2
        x0 = max(0, int(x0 - extra)); x1 = min(img.width, int(x1 + extra))
    cropped = img.crop((x0, y0, x1, y1))
    cropped = cropped.resize((img.width, img.height), Image.LANCZOS)
    cropped.save(out_path, optimize=True)
    return f"ok asset={w}x{h} ({100*w*h/(img.width*img.height):.1f}% of full)"


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--app", choices=("freecad","blender"), required=True)
    p.add_argument("--in-dir", required=True)
    p.add_argument("--in-glob", default="D_*loaded*.png")
    p.add_argument("--out-prefix", default="E_")
    args = p.parse_args()
    in_dir = Path(args.in_dir)
    n_ok = n_skip = n_fail = 0
    for src in sorted(in_dir.glob(args.in_glob)):
        out_name = args.out_prefix + src.name[len("D_"):]
        out = in_dir / out_name
        if out.exists():
            n_skip += 1
            continue
        res = zoom_to_asset(src, out, args.app)
        if res.startswith("ok"):
            n_ok += 1
        else:
            n_fail += 1
            print(f"  FAIL {src.name[:60]}  →  {res}")
        if (n_ok + n_fail) % 25 == 0:
            print(f"  ... {n_ok + n_fail} done")
    print(f"\n{n_ok} processed, {n_skip} skipped, {n_fail} failed")
