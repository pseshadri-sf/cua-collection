"""Crop each goal screenshot tightly to the rendered asset.

Both FC and BL render their viewport background as dark gray (~50 RGB) under
Mesa software OpenGL; the asset is shaded geometry (>90 brightness).

Detection (two passes, designed to avoid cut-off):
  1. ROUGH-CENTER pass: project the fg mask onto X/Y with a high DENSE_FRAC
     (0.20) to find the asset's dense body. This ignores scattered noise.
  2. EXPAND-OUTWARD pass: starting from the rough bbox, walk each side
     outward as long as the column/row still has at least a few fg pixels
     (LOW_DENSE_FRAC=0.02). Stops as soon as we hit ≥`GAP_LIMIT` consecutive
     near-empty cols/rows. This recovers thin extremities of the asset
     (curves, edges, thin features) without bleeding into far-away gizmos.

Adaptive margin: smaller assets get tighter margin (more zoom in the final
upscaled image); larger assets get more breathing room. Scales linearly
between MARGIN_SMALL at min_size and MARGIN_LARGE at max_size of the
viewport.

Gizmo masking (Blender): the top-right navigation gizmo and top-left
scene-info overlay are explicitly zeroed in the mask so they cannot
contribute to detection even at LOW_DENSE_FRAC.

Output: D_*loaded*.png → E_*loaded*.png alongside originals.
"""
from __future__ import annotations
from pathlib import Path
from PIL import Image

# Viewport bounds per app. FreeCAD chrome already reserves edge space.
# Blender viewport extends further; we mask gizmos separately below.
VIEWPORTS = {
    "freecad": (340, 110, 1900, 1040),
    "blender": (180, 110, 1440, 970),
}
# Regions inside the viewport that are UI gizmos, not asset. Coordinates are
# in CROP-LOCAL space (after VIEWPORTS crop). Format: list of (x0,y0,x1,y1).
GIZMO_MASKS = {
    "freecad": [],  # no major in-viewport gizmos under our default config
    "blender": [
        (1080, 0, 1260, 200),   # top-right navigation gizmo (after 180px viewport L)
        (0,    0,  120,  60),   # top-left overlay text
    ],
}
BG_THRESHOLD = 90
# Detection thresholds: rough bbox uses HIGH to avoid noise; expansion uses LOW.
DENSE_FRAC_ROUGH = {"freecad": 0.05, "blender": 0.20}
DENSE_FRAC_LOW   = {"freecad": 0.01, "blender": 0.02}
GAP_LIMIT = 60  # stop expanding after this many near-empty cols/rows


def _dense_range(counts, dense_frac):
    if not counts: return None
    peak = max(counts)
    if peak == 0: return None
    cutoff = max(1, int(peak * dense_frac))
    idx = [i for i, c in enumerate(counts) if c >= cutoff]
    return (idx[0], idx[-1] + 1) if idx else None


def _expand(rough_lo, rough_hi, counts, low_frac):
    """Extend [rough_lo, rough_hi) outward as long as columns/rows still
    have at least low_frac*peak fg pixels. Stop after GAP_LIMIT empties."""
    peak = max(counts) if counts else 0
    if peak == 0: return rough_lo, rough_hi
    cutoff = max(1, int(peak * low_frac))
    n = len(counts)
    # Expand left
    lo = rough_lo
    gap = 0
    for i in range(rough_lo - 1, -1, -1):
        if counts[i] >= cutoff:
            lo = i
            gap = 0
        else:
            gap += 1
            if gap >= GAP_LIMIT: break
    # Expand right
    hi = rough_hi
    gap = 0
    for i in range(rough_hi, n):
        if counts[i] >= cutoff:
            hi = i + 1
            gap = 0
        else:
            gap += 1
            if gap >= GAP_LIMIT: break
    return lo, hi


def find_asset_bbox(img, app):
    L, T, R, B = VIEWPORTS[app]
    crop = img.crop((L, T, R, B)).convert("L")
    W, H = crop.size
    px = crop.load()
    # Mask gizmos (set those pixels to bg so they don't contribute)
    for gx0, gy0, gx1, gy1 in GIZMO_MASKS[app]:
        for y in range(max(0, gy0), min(H, gy1)):
            for x in range(max(0, gx0), min(W, gx1)):
                if px[x, y] > BG_THRESHOLD:
                    px[x, y] = 0
    # Build column/row fg counts
    col_counts = [0] * W
    row_counts = [0] * H
    for y in range(H):
        for x in range(W):
            if px[x, y] > BG_THRESHOLD:
                col_counts[x] += 1
                row_counts[y] += 1
    # Rough bbox (dense central body)
    xr = _dense_range(col_counts, DENSE_FRAC_ROUGH[app])
    yr = _dense_range(row_counts, DENSE_FRAC_ROUGH[app])
    if xr is None or yr is None: return None
    # Expand outward to recover thin extremities
    xr = _expand(xr[0], xr[1], col_counts, DENSE_FRAC_LOW[app])
    yr = _expand(yr[0], yr[1], row_counts, DENSE_FRAC_LOW[app])
    return (xr[0] + L, yr[0] + T, xr[1] + L, yr[1] + T)


def _adaptive_margin(bbox_w, bbox_h, full_w, full_h):
    """Smaller assets → smaller margin (more zoom); larger assets → bigger
    margin (less zoom). Sized by AREA fraction so tall-thin shapes (helix,
    tree silhouette) are correctly classified as small even when one
    dimension nearly fills the viewport.
    """
    area_frac = (bbox_w * bbox_h) / (full_w * full_h)
    # Bounds:
    #   area ≤ 2%  (tiny)        → 4%  margin  → max zoom
    #   area ≥ 30% (fills frame) → 18% margin  → gentle zoom
    if area_frac <= 0.02:
        return 0.04
    if area_frac >= 0.30:
        return 0.18
    # Linear interp on sqrt(area) so the curve isn't dominated by big assets
    import math
    t = (math.sqrt(area_frac) - math.sqrt(0.02)) / (math.sqrt(0.30) - math.sqrt(0.02))
    return 0.04 + t * (0.18 - 0.04)


def zoom_to_asset(in_path, out_path, app):
    img = Image.open(in_path).convert("RGB")
    bbox = find_asset_bbox(img, app)
    if bbox is None: return "no-asset-found"
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    if max(w, h) < 50: return f"too-small ({w}x{h})"
    margin = _adaptive_margin(w, h, img.width, img.height)
    mx, my = int(w * margin), int(h * margin)
    x0 = max(0, x0 - mx); y0 = max(0, y0 - my)
    x1 = min(img.width, x1 + mx); y1 = min(img.height, y1 + my)
    aspect = img.width / img.height
    bw, bh = x1 - x0, y1 - y0
    if bw / bh > aspect:
        extra = (bw / aspect - bh) / 2
        y0 = max(0, int(y0 - extra)); y1 = min(img.height, int(y1 + extra))
    else:
        extra = (bh * aspect - bw) / 2
        x0 = max(0, int(x0 - extra)); x1 = min(img.width, int(x1 + extra))
    img.crop((x0, y0, x1, y1)).resize((img.width, img.height), Image.LANCZOS).save(out_path, optimize=True)
    return f"ok asset={w}x{h} margin={margin:.2f} ({100*w*h/(img.width*img.height):.1f}%)"


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--app", choices=("freecad","blender"), required=True)
    p.add_argument("--in-dir", required=True)
    p.add_argument("--in-glob", default="D_*loaded*.png")
    p.add_argument("--out-prefix", default="E_")
    args = p.parse_args()
    in_dir = Path(args.in_dir)
    n_ok = n_fail = 0
    for src in sorted(in_dir.glob(args.in_glob)):
        out = in_dir / (args.out_prefix + src.name[len(args.in_glob.split('*')[0]):])
        res = zoom_to_asset(src, out, args.app)
        if res.startswith("ok"): n_ok += 1
        else:
            n_fail += 1
            print(f"  FAIL {src.name[:60]:<60}  →  {res}")
    print(f"\n{n_ok} processed, {n_fail} failed")
