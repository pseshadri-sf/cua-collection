"""Crop each goal screenshot tightly to the rendered asset.

Both FC and BL render their viewport background as dark gray (~50 RGB) under
Mesa software OpenGL; the asset is shaded geometry (>90 brightness). We
project the foreground mask onto X/Y axes and crop to the dense range.

App-specific tuning:
  - FreeCAD: looser DENSE_FRAC=0.05 + 10% margin — FC chrome reserves edge
    space anyway, so the bbox is already conservative.
  - Blender: tighter DENSE_FRAC=0.20 + 5% margin + viewport excludes the
    top-right navigation gizmo and top-left overlay text, which would
    otherwise bloat the bbox to the full viewport width.

Output: D_*loaded*.png → E_*loaded*.png alongside originals.
Failed crops (blank frames, edge-on profiles) are skipped — the original
D_ remains as the only goal for those, surfacing the failure.
"""
from __future__ import annotations
from pathlib import Path
from PIL import Image

VIEWPORTS = {
    "freecad": (340, 110, 1900, 1040),
    "blender": (180, 110, 1440, 970),
}
BG_THRESHOLD = 90
DENSE_FRAC = {"freecad": 0.05, "blender": 0.20}
MARGIN_FRAC = {"freecad": 0.10, "blender": 0.05}


def _dense_range(counts, dense_frac):
    if not counts: return None
    peak = max(counts)
    if peak == 0: return None
    cutoff = max(1, int(peak * dense_frac))
    idx = [i for i, c in enumerate(counts) if c >= cutoff]
    return (idx[0], idx[-1] + 1) if idx else None


def find_asset_bbox(img, app):
    L, T, R, B = VIEWPORTS[app]
    crop = img.crop((L, T, R, B)).convert("L")
    W, H = crop.size
    px = crop.load()
    col_counts = [0] * W
    row_counts = [0] * H
    for y in range(H):
        for x in range(W):
            if px[x, y] > BG_THRESHOLD:
                col_counts[x] += 1
                row_counts[y] += 1
    xr = _dense_range(col_counts, DENSE_FRAC[app])
    yr = _dense_range(row_counts, DENSE_FRAC[app])
    if xr is None or yr is None: return None
    return (xr[0] + L, yr[0] + T, xr[1] + L, yr[1] + T)


def zoom_to_asset(in_path, out_path, app):
    img = Image.open(in_path).convert("RGB")
    bbox = find_asset_bbox(img, app)
    if bbox is None: return "no-asset-found"
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    if w < 50 or h < 50: return f"too-small ({w}x{h})"
    mx, my = int(w * MARGIN_FRAC[app]), int(h * MARGIN_FRAC[app])
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
    return f"ok asset={w}x{h} ({100*w*h/(img.width*img.height):.1f}%)"


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
