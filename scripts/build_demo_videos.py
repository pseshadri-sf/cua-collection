"""Condense each trajectory.mp4 into a ~2-minute demo video.

For each run dir with both trajectory.json and trajectory.mp4:
  - Read step action_time_seconds (each step's AFTER frame timestamp).
  - Take a clip of length per_step_seconds centred on each action_time
    (clamped to video bounds; coalesced if adjacent).
  - Concatenate with ffmpeg into video_post.mp4 in the same dir.

Skips any run whose trajectory.json has no agent actions (init-only).
Skips any run whose trajectory.mp4 is missing or 0 bytes.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def video_duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        text=True,
    ).strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def cut_segment(src: Path, start: float, end: float, out: Path) -> None:
    """Re-encode a fast h264 segment with no audio."""
    duration = max(0.1, end - start)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-ss", f"{start:.2f}", "-t", f"{duration:.2f}",
         "-i", str(src),
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
         "-pix_fmt", "yuv420p", "-an",
         str(out)],
        check=True,
    )


def concat_segments(parts: list[Path], out: Path) -> None:
    list_path = out.parent / ".concat.txt"
    list_path.write_text("\n".join(f"file '{p.resolve()}'" for p in parts))
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(list_path),
         "-c", "copy", str(out)],
        check=True,
    )
    list_path.unlink(missing_ok=True)


def plan_clips(action_times: list[float], video_dur: float,
               target_seconds: float = 120.0,
               min_clip: float = 1.5,
               max_clip: float = 6.0) -> list[tuple[float, float]]:
    """Centre each action_time in a clip; coalesce overlapping ones."""
    n = len(action_times)
    if n == 0:
        return []
    per = max(min_clip, min(target_seconds / n, max_clip))
    half = per / 2
    raw = [
        (max(0.0, t - half), min(video_dur, t + half))
        for t in sorted(action_times)
    ]
    # Coalesce overlapping ranges (some action_times are <0.5s apart).
    merged: list[list[float]] = []
    for s, e in raw:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def build_one(run_dir: Path, target_seconds: float = 120.0) -> str | None:
    traj_path = run_dir / "trajectory.json"
    video_path = run_dir / "trajectory.mp4"
    out_path = run_dir / "video_post.mp4"
    if not traj_path.exists():
        return f"skip {run_dir.name}: no trajectory.json"
    if not video_path.exists() or video_path.stat().st_size == 0:
        return f"skip {run_dir.name}: no video"

    try:
        traj = json.loads(traj_path.read_text())
    except json.JSONDecodeError as exc:
        return f"skip {run_dir.name}: bad json: {exc}"

    # action_time_seconds is the AFTER-frame of each agent action.
    # Step 0 is the init at 0.0; skip it.
    times = [s["action_time_seconds"] for s in traj.get("trajectory", [])
             if s.get("step_idx", 0) > 0 and s.get("action_time_seconds") is not None]
    if not times:
        return f"skip {run_dir.name}: 0 agent actions"

    dur = video_duration(video_path)
    if dur <= 0:
        return f"skip {run_dir.name}: zero-duration video"

    clips = plan_clips(times, dur, target_seconds=target_seconds)

    with tempfile.TemporaryDirectory(prefix="vidcut_", dir=run_dir) as td:
        td_path = Path(td)
        parts = []
        for i, (s, e) in enumerate(clips):
            part = td_path / f"part_{i:03d}.mp4"
            try:
                cut_segment(video_path, s, e, part)
            except subprocess.CalledProcessError:
                continue
            parts.append(part)
        if not parts:
            return f"skip {run_dir.name}: no cuts produced"
        try:
            concat_segments(parts, out_path)
        except subprocess.CalledProcessError as exc:
            return f"err  {run_dir.name}: concat failed: {exc}"

    new_dur = video_duration(out_path)
    return f"ok   {run_dir.name}: {len(times)} steps, src {dur:.0f}s -> {new_dur:.0f}s, {out_path}"


def discover(root: Path) -> list[Path]:
    """Find all directories with trajectory.json+trajectory.mp4, recursive."""
    hits = []
    for traj in sorted(root.rglob("trajectory.json")):
        run_dir = traj.parent
        if (run_dir / "trajectory.mp4").exists():
            hits.append(run_dir)
    return hits


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("root", type=Path, nargs="?",
                   default=Path.home() / "cua_agent_runs",
                   help="root to scan recursively for run dirs")
    p.add_argument("--target-seconds", type=float, default=120.0,
                   help="target output length per video (default 120s)")
    p.add_argument("--force", action="store_true",
                   help="overwrite existing video_post.mp4")
    args = p.parse_args(argv)

    runs = discover(args.root)
    print(f"found {len(runs)} run dirs under {args.root}")
    for run_dir in runs:
        if (run_dir / "video_post.mp4").exists() and not args.force:
            print(f"skip {run_dir.name}: video_post.mp4 already exists "
                  f"(use --force to overwrite)")
            continue
        result = build_one(run_dir, target_seconds=args.target_seconds)
        if result:
            print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
