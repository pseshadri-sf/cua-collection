"""Trajectory video post-processing (--postprocess).

Turns the raw real-time GUI screen-recording into a CLEAN "viewfinder" video that
isolates the EFFECT of each code block on the asset, with minimal noise:

  1. CROP to the 3D viewport rectangle (drops the console / model-tree / property
     panels — i.e. the code-entry & GUI chrome).
  2. CUT to a tight keep-window around each code block's settled "after" moment
     (its `action_time_seconds`), dropping the dead time spent typing code and
     navigating the GUI between components.
  3. CONCAT the windows -> the asset appears component-by-component.
  4. Emit recalibrated timestamps (code action + reasoning/CoT) on the NEW,
     shortened timeline in `<out>.meta.json`.

Per-app: FreeCAD keeps a large 3D view throughout (console is a panel), so a
wider window captures the transition. Blender shows the framed Layout viewport
only around the capture (python_eval works in the small Scripting viewport), so
the window is anchored tightly on `action_time`.

Outputs `video_clean.mp4` + `video_clean.meta.json` in the job dir.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# Viewport crop rect per app on a 1920x1080 capture: (x, y, w, h).
_CROP = {
    "freecad": (320, 110, 1575, 600),   # FreeCAD 3D view (excl. tree/toolbar/console)
    "blender": (80, 92, 1410, 878),     # Blender Layout 3D viewport (excl. panels/header/timeline)
    "kicad":   (345, 130, 1295, 890),   # pcbnew canvas only (excl. left/right panels, toolbars, status) — the console is parked off-screen
}
# Keep-window around each step's settled `action_time`: (pre, post) seconds.
_WINDOW = {
    "freecad": (1.2, 0.8),
    "blender": (0.5, 1.2),
    # KiCad compositional build dwells ~1.2s before recording action_time and
    # ~1.0s after; the console is off-screen, so both sides are clean canvas.
    "kicad":   (1.0, 1.0),
}


def _ffprobe_duration(video: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def _code_steps(traj: dict, app: str) -> list[dict]:
    """Code-bearing steps with a usable video timestamp, in order."""
    out = []
    for s in traj.get("trajectory", []):
        a = s.get("action") or {}
        t = s.get("action_time_seconds")
        if t is None:
            continue
        typ = a.get("type")
        if typ in ("python_eval", "pcbnew_eval"):
            code = a.get("code")
        elif typ == "type":
            code = a.get("text")
        else:
            code = None
        if typ in ("python_eval", "pcbnew_eval", "type") and code:
            out.append({"step_idx": s.get("step_idx"), "t": float(t), "code": code,
                        "rationale": s.get("rationale"),
                        "reasoning_trace": s.get("reasoning_trace")})
    return out


def postprocess_trajectory(job_dir: str | Path, app: str,
                           video_name: str = "trajectory.mp4") -> dict | None:
    """Produce job_dir/video_clean.mp4 + video_clean.meta.json. Returns the meta
    dict, or None if it could not run (no video / no timed code steps / no ffmpeg)."""
    job = Path(job_dir)
    src = job / video_name
    tj = job / "trajectory.json"
    if not src.exists() or not tj.exists() or not shutil.which("ffmpeg"):
        return None
    if app.startswith("ki") or app == "kicad":
        app = "kicad"
    elif app.startswith("bl") or app == "blender":
        app = "blender"
    else:
        app = "freecad"
    try:
        traj = json.loads(tj.read_text())
    except Exception:
        return None
    steps = _code_steps(traj, app)
    if not steps:
        return None
    dur = _ffprobe_duration(src)
    if dur <= 0:
        return None
    cx, cy, cw, ch = _CROP[app]
    pre, post = _WINDOW[app]
    crop = f"crop={cw}:{ch}:{cx}:{cy}"

    work = Path(tempfile.mkdtemp(prefix="pp_"))
    clips = []
    new_steps = []
    cum = 0.0
    try:
        for k, st in enumerate(steps):
            start = max(0.0, st["t"] - pre)
            end = min(dur, st["t"] + post)
            if end - start < 0.2:
                continue
            clip = work / f"c{k:03d}.mp4"
            r = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}",
                 "-to", f"{end:.3f}", "-i", str(src), "-vf", crop,
                 "-an", "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p", str(clip)],
                capture_output=True, timeout=120)
            if not clip.exists() or clip.stat().st_size == 0:
                continue
            wlen = end - start
            # the code block's effect is settled at offset `pre` within this window
            action_t = cum + min(pre, wlen)
            new_steps.append({
                "step_idx": st["step_idx"],
                "video_time_s": round(action_t, 3),
                "window": [round(cum, 3), round(cum + wlen, 3)],
                "code": st["code"],
                "rationale": st["rationale"],
                "reasoning_trace": st["reasoning_trace"],
            })
            clips.append(clip)
            cum += wlen
        if not clips:
            return None
        concat = work / "list.txt"
        concat.write_text("".join(f"file '{c}'\n" for c in clips))
        out_mp4 = job / "video_clean.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(concat), "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", str(out_mp4)],
            capture_output=True, timeout=300)
        if not out_mp4.exists() or out_mp4.stat().st_size == 0:
            return None
        meta = {
            "source_video": video_name,
            "app": app,
            "crop_xywh": [cx, cy, cw, ch],
            "window_pre_post_s": [pre, post],
            "clean_duration_s": round(cum, 3),
            "n_code_blocks": len(new_steps),
            "note": "viewfinder-only; code-entry/GUI navigation cut. video_time_s is each "
                    "code block's settled moment on the CLEAN timeline.",
            "steps": new_steps,
        }
        (job / "video_clean.meta.json").write_text(json.dumps(meta, indent=2))
        return meta
    finally:
        shutil.rmtree(work, ignore_errors=True)
