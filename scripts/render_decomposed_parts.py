"""Render each decomposed sub-part of an asset to a small PNG thumbnail.

Reads the sidecar JSON for the goal asset, finds its corresponding
`decomposed/<asset_stem>/manifest.json`, then renders each part_NN.step (FC)
or part_NN.blend (BL) into the same `parts_cache/<asset_stem>/part_NN.png`.

Cache is global (under the project tmp dir) so multiple benchmark runs that
share assets pay the rendering cost once.

Usage:
    python render_decomposed_parts.py --sidecar <goal.meta.json> [--res 320x240]
    python render_decomposed_parts.py --jobs-file wave6_jobs.jsonl
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_RENDER = _REPO / "scripts" / "render_agent_model.py"
_CACHE_ROOT = Path("/tmp/decomposed_parts_cache")


def _asset_stem(meta: dict) -> str | None:
    asset = meta.get("asset")
    if not asset:
        return None
    return Path(asset).stem


def _decomposed_dir_for(meta: dict) -> Path | None:
    """Return the on-disk decomposed dir for the asset, if it exists.

    The decomposer writes to `<asset_parent_parent>/decomposed/<stem>/`.
    e.g. `/home/ubuntu/cua_gui_smoketest/decomposed/<stem>/`.
    """
    asset = Path(meta.get("asset") or "")
    stem = _asset_stem(meta)
    if not stem:
        return None
    # asset is under cua_xxx_smoketest/assets/, so parent.parent is cua_xxx_smoketest
    return asset.parent.parent / "decomposed" / stem


def render_part_fc(part_step: Path, png_out: Path, res: str) -> bool:
    """Convert STEP -> STL via freecadcmd, then render with xvfb-run blender."""
    with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as fh:
        stl = Path(fh.name)
    try:
        env = {**os.environ, "MODEL_IN": str(part_step), "MODEL_OUT": str(stl)}
        r = subprocess.run(["freecadcmd", str(_RENDER)],
                           env=env, timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode != 0 or not stl.exists() or stl.stat().st_size == 0:
            return False
        env = {**os.environ, "MODE": "stl", "MODEL_IN": str(stl),
               "MODEL_OUT": str(png_out), "RES": res}
        r = subprocess.run(["xvfb-run", "-a", "--server-args=-screen 0 800x600x24",
                            "blender", "-b", "-noaudio", "-P", str(_RENDER)],
                           env=env, timeout=90,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return r.returncode == 0 and png_out.exists()
    finally:
        if stl.exists(): stl.unlink()


def render_part_bl(part_blend: Path, png_out: Path, res: str) -> bool:
    env = {**os.environ, "MODE": "blend", "MODEL_IN": str(part_blend),
           "MODEL_OUT": str(png_out), "RES": res}
    r = subprocess.run(["xvfb-run", "-a", "--server-args=-screen 0 800x600x24",
                        "blender", "-b", "-noaudio", "-P", str(_RENDER)],
                       env=env, timeout=90,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return r.returncode == 0 and png_out.exists()


def render_all_parts(sidecar: Path, res: str = "320x240",
                      force: bool = False) -> dict:
    """Render every part for one sidecar. Returns map {part_name: png_path or None}."""
    meta = json.loads(sidecar.read_text())
    parts = meta.get("parts") or []
    out: dict[str, str | None] = {}
    if not parts:
        return out
    stem = _asset_stem(meta)
    if not stem:
        return out
    dec_dir = _decomposed_dir_for(meta)
    if not dec_dir or not dec_dir.is_dir():
        return out
    cache = _CACHE_ROOT / stem
    cache.mkdir(parents=True, exist_ok=True)
    app = meta.get("app", "freecad")
    ext = ".step" if app == "freecad" else ".blend"
    for p in parts:
        idx = p.get("index")
        if idx is None: continue
        part_file = dec_dir / f"part_{idx:02d}{ext}"
        if not part_file.exists():
            out[p.get("name", f"part_{idx:02d}")] = None
            continue
        png = cache / f"part_{idx:02d}.png"
        if not png.exists() or force:
            ok = (render_part_fc(part_file, png, res) if app == "freecad"
                  else render_part_bl(part_file, png, res))
            if not ok:
                out[p.get("name", f"part_{idx:02d}")] = None
                continue
        out[p.get("name", f"part_{idx:02d}")] = str(png)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sidecar")
    g.add_argument("--jobs-file")
    ap.add_argument("--res", default="320x240")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    if args.sidecar:
        out = render_all_parts(Path(args.sidecar), args.res, args.force)
        print(json.dumps(out, indent=2))
        return 0
    # jobs-file mode
    seen = set()
    n_ok = n_skip = 0
    with open(args.jobs_file) as fh:
        for line in fh:
            line = line.strip()
            if not line: continue
            j = json.loads(line)
            gp = Path(j["goal_path"])
            sc = gp.with_suffix(".meta.json")
            if not sc.exists(): continue
            stem = _asset_stem(json.loads(sc.read_text())) or ""
            if stem in seen: continue
            seen.add(stem)
            out = render_all_parts(sc, args.res, args.force)
            if out:
                n_ok += 1
                print(f"[ok ] {stem}  parts={len(out)} ({sum(1 for v in out.values() if v)} rendered)")
            else:
                n_skip += 1
    print(f"DONE ok={n_ok} skip={n_skip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
