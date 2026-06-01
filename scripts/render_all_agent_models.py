"""Walk a parallel-orchestrator run dir and produce one clean agent-model
render per job at `<job>/eval/agent_render.png`.

Strategy:
  FC asset (eval/agent_model.FCStd): freecadcmd exports to /tmp .stl,
      then xvfb-run blender -b loads + renders the STL.
  BL asset (eval/agent_model.blend): xvfb-run blender -b opens + renders
      the .blend directly.

Skips jobs where agent_render.png already exists (idempotent).
"""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_RENDER = _REPO / "scripts" / "render_agent_model.py"


def render_fc(fcstd: Path, png_out: Path, res: str) -> bool:
    with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as fh:
        stl_path = Path(fh.name)
    try:
        env = {**os.environ, "MODEL_IN": str(fcstd), "MODEL_OUT": str(stl_path)}
        r = subprocess.run(["freecadcmd", str(_RENDER)],
                           env=env, timeout=180,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0 or not stl_path.exists() or stl_path.stat().st_size == 0:
            print(f"  [fc-export err] {fcstd.parent.parent.name}: rc={r.returncode}")
            return False
        env = {**os.environ, "MODE": "stl", "MODEL_IN": str(stl_path),
               "MODEL_OUT": str(png_out), "RES": res}
        r = subprocess.run(["xvfb-run", "-a", "--server-args=-screen 0 800x600x24",
                            "blender", "-b", "-noaudio", "-P", str(_RENDER)],
                           env=env, timeout=180,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0 or not png_out.exists():
            print(f"  [bl-render err] {fcstd.parent.parent.name}: rc={r.returncode}")
            return False
        return True
    finally:
        if stl_path.exists(): stl_path.unlink()


def render_bl(blend: Path, png_out: Path, res: str) -> bool:
    env = {**os.environ, "MODE": "blend", "MODEL_IN": str(blend),
           "MODEL_OUT": str(png_out), "RES": res}
    r = subprocess.run(["xvfb-run", "-a", "--server-args=-screen 0 800x600x24",
                        "blender", "-b", "-noaudio", "-P", str(_RENDER)],
                       env=env, timeout=180,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0 or not png_out.exists():
        print(f"  [bl-render err] {blend.parent.parent.name}: rc={r.returncode} {r.stderr.decode()[:200]}")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run_dir", type=Path)
    p.add_argument("--res", default="640x480")
    p.add_argument("--force", action="store_true")
    args = p.parse_args(argv)

    n_ok = n_skip = n_err = 0
    for job_dir in sorted(args.run_dir.glob("worker_*/jobs/*")):
        ed = job_dir / "eval"
        if not ed.is_dir(): continue
        png = ed / "agent_render.png"
        if png.exists() and not args.force:
            n_skip += 1; continue
        fc = ed / "agent_model.FCStd"
        bl = ed / "agent_model.blend"
        if fc.exists():
            ok = render_fc(fc, png, args.res)
        elif bl.exists():
            ok = render_bl(bl, png, args.res)
        else:
            n_err += 1; continue
        if ok:
            n_ok += 1; print(f"  [ok ] {job_dir.name}")
        else:
            n_err += 1

    print(f"\nDONE ok={n_ok} skip={n_skip} err={n_err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
