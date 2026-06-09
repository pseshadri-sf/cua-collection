"""Batch post-process every trajectory in a run dir into clean viewfinder videos.

    uv run python scripts/postprocess_run.py <run_dir> [--workers 8] [--app auto|freecad|blender]

Writes video_clean.mp4 + video_clean.meta.json next to each trajectory.mp4.
App is inferred per job from the job_id prefix (fx_/bx_/fc_/bl_) unless forced.
"""
import argparse
import concurrent.futures as cf
import glob
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cua_smoketest.agent.postprocess import postprocess_trajectory  # noqa: E402


def infer_app(jid: str, forced: str) -> str:
    if forced != "auto":
        return forced
    return "blender" if jid.startswith(("bx", "bl")) else "freecad"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--app", choices=("auto", "freecad", "blender"), default="auto")
    args = ap.parse_args()

    jobs = [Path(p) for p in glob.glob(args.run_dir + "/worker_*/jobs/*")
            if os.path.isdir(p) and os.path.exists(os.path.join(p, "trajectory.mp4"))]
    print(f"post-processing {len(jobs)} trajectories ({args.workers} workers)…")

    def one(jd: Path):
        try:
            m = postprocess_trajectory(jd, infer_app(jd.name, args.app))
            return jd.name, (m["n_code_blocks"], m["clean_duration_s"]) if m else None
        except Exception as e:  # noqa: BLE001
            return jd.name, ("err", str(e)[:60])

    ok = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, (name, r) in enumerate(ex.map(one, jobs), 1):
            if r and r[0] != "err":
                ok += 1
            if i % 50 == 0:
                print(f"  {i}/{len(jobs)} done ({ok} clean videos)…", flush=True)
    print(f"DONE: {ok}/{len(jobs)} clean videos written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
