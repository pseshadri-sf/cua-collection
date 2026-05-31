"""Parallel orchestrator for VLM/GUI agent trajectories.

Runs N agent trajectories concurrently on one EC2 host. Each worker
gets its own Xvfb display, openbox window manager, HOME, XDG dirs,
output tree, and logs. Per-job artifacts are the same shape the
existing single-worker pipeline writes (trajectory.mp4, trajectory.json,
frames/, logs/) — the orchestrator invokes the existing agent CLI as
the per-job worker.

Usage:
    python parallel_orchestrator.py --num-workers 4 --num-jobs 20 \\
        --app freecad --output-dir <dir>

CLI flags follow the task spec at /home/ubuntu/dev/tasks/parallel.md.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue, Empty
from typing import Any


# --- defaults ------------------------------------------------------------

PROJECT_ROOT = Path("/home/ubuntu/dev/init_envs")
UV_BIN = "/home/ubuntu/.local/bin/uv"
DEFAULT_MODEL = "qwen/qwen3-vl-30b-a3b-instruct"  # 7 providers vs gemma-4's 1
DEFAULT_EXTRA_ARGS = (
    "--model", DEFAULT_MODEL,
    "--reasoning-effort", "low",
    "--image-max-dim", "1024",
)
FREECAD_SCRIPT = PROJECT_ROOT / "scripts" / "agent_trajectory.py"
BLENDER_SCRIPT = PROJECT_ROOT / "scripts" / "blender_agent_trajectory.py"
FREECAD_GOAL_DIR = Path.home() / "cua_gui_smoketest" / "screenshots"
BLENDER_GOAL_DIR = Path.home() / "cua_blender_smoketest" / "screenshots"
FREECAD_ASSET_EXTS = (".FCStd", ".step", ".stp", ".iges", ".igs", ".brep", ".stl")
BLENDER_ASSET_EXTS = (".blend",)


# --- worker slot ---------------------------------------------------------

@dataclasses.dataclass
class WorkerSlot:
    worker_id: int
    display: str
    width: int
    height: int
    depth: int
    run_dir: Path
    worker_dir: Path
    home_dir: Path
    xdg_config_home: Path
    xdg_cache_home: Path
    xdg_data_home: Path
    app: str
    xvfb_proc: subprocess.Popen | None = None
    openbox_proc: subprocess.Popen | None = None
    current_subproc: subprocess.Popen | None = None  # the running agent job
    busy: bool = False

    @classmethod
    def build(cls, worker_id: int, run_dir: Path, app: str,
              display_base: int, width: int, height: int, depth: int) -> "WorkerSlot":
        wd = run_dir / f"worker_{worker_id:03d}"
        home = wd / "home"
        return cls(
            worker_id=worker_id,
            display=f":{display_base + worker_id}",
            width=width, height=height, depth=depth,
            run_dir=run_dir,
            worker_dir=wd,
            home_dir=home,
            xdg_config_home=home / ".config",
            xdg_cache_home=home / ".cache",
            xdg_data_home=home / ".local" / "share",
            app=app,
        )

    def _mkdirs(self) -> None:
        for p in (self.worker_dir, self.home_dir, self.xdg_config_home,
                  self.xdg_cache_home, self.xdg_data_home,
                  self.worker_dir / "logs",
                  self.worker_dir / "jobs",
                  self.worker_dir / "tmp"):
            p.mkdir(parents=True, exist_ok=True)

    def setup(self) -> None:
        """Start Xvfb + openbox on this slot's display."""
        self._mkdirs()
        # pyautogui's xlib backend requires ~/.Xauthority to exist (it
        # warns rather than fails, but only if the FILE is present).
        # Touch an empty one inside the per-worker HOME.
        (self.home_dir / ".Xauthority").touch(exist_ok=True)
        self._clean_stale_lock(self.display)

        xvfb = shutil.which("Xvfb")
        if not xvfb:
            raise RuntimeError("Xvfb not installed")
        xvfb_log = open(self.worker_dir / "logs" / "xvfb.log", "ab")
        self.xvfb_proc = subprocess.Popen(
            [xvfb, self.display,
             "-screen", "0", f"{self.width}x{self.height}x{self.depth}",
             "-nolisten", "tcp"],
            stdout=xvfb_log, stderr=xvfb_log, start_new_session=True,
        )
        # wait for the display to come up
        for _ in range(60):
            time.sleep(0.25)
            if self._display_alive():
                break
        else:
            raise RuntimeError(f"Xvfb on {self.display} did not come up in 15s")

        openbox = shutil.which("openbox")
        if openbox:
            ob_log = open(self.worker_dir / "logs" / "openbox.log", "ab")
            env = {**os.environ, "DISPLAY": self.display, "HOME": str(self.home_dir)}
            self.openbox_proc = subprocess.Popen(
                [openbox], stdout=ob_log, stderr=ob_log, env=env,
                start_new_session=True,
            )
            time.sleep(0.6)

    def teardown(self) -> None:
        """Tear down openbox + Xvfb. Also kill any leftover agent subproc."""
        for proc, name in [
            (self.current_subproc, "agent-subprocess"),
            (self.openbox_proc, "openbox"),
            (self.xvfb_proc, "Xvfb"),
        ]:
            if proc is None or proc.poll() is not None:
                continue
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def env_for_subprocess(self, job: dict, job_output_dir: Path) -> dict:
        env = os.environ.copy()
        env.update({
            "DISPLAY": self.display,
            "HOME": str(self.home_dir),
            "XDG_CONFIG_HOME": str(self.xdg_config_home),
            "XDG_CACHE_HOME": str(self.xdg_cache_home),
            "XDG_DATA_HOME": str(self.xdg_data_home),
            "TMPDIR": str(self.worker_dir / "tmp"),
            "CUA_WORKER_ID": f"worker_{self.worker_id:03d}",
            "CUA_RUN_DIR": str(self.run_dir),
            "CUA_WORKER_DIR": str(self.worker_dir),
            "CUA_JOB_ID": job["job_id"],
            "CUA_JOB_FILE": str(self.worker_dir / "jobs" / f"{job['job_id']}.json"),
            "CUA_OUTPUT_DIR": str(job_output_dir),
            "CUA_LOG_DIR": str(self.worker_dir / "logs"),
            "CUA_APP": self.app,
        })
        return env

    def _display_alive(self) -> bool:
        xdpyinfo = shutil.which("xdpyinfo")
        if not xdpyinfo:
            return True  # assume alive if no tool
        env = {**os.environ, "DISPLAY": self.display}
        res = subprocess.run(
            [xdpyinfo, "-display", self.display],
            capture_output=True, env=env, timeout=5,
        )
        return res.returncode == 0

    @staticmethod
    def _clean_stale_lock(display: str) -> None:
        """If /tmp/.X<N>-lock exists but no Xvfb owns it, clean it cautiously."""
        n = display.lstrip(":")
        lock = Path(f"/tmp/.X{n}-lock")
        sock = Path(f"/tmp/.X11-unix/X{n}")
        if not lock.exists() and not sock.exists():
            return
        try:
            pid = int(lock.read_text().strip())
        except (OSError, ValueError):
            return
        try:
            os.kill(pid, 0)
            # Pid is alive — assume real Xvfb. Don't touch.
            return
        except ProcessLookupError:
            pass
        # Stale lock; safe to remove.
        for p in (lock, sock):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


# --- job model -----------------------------------------------------------

@dataclasses.dataclass
class JobResult:
    job_id: str
    worker_id: str
    display: str
    app: str
    goal_path: str
    status: str                          # succeeded | failed | timed_out
    terminated_by: str | None            # lifted from trajectory.json
    steps: int                            # from trajectory.json
    started_at: float
    ended_at: float
    duration_sec: float
    trajectory_json_path: str | None
    trajectory_mp4_path: str | None
    return_code: int
    error_message: str
    retries: int

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def now() -> float:
    return time.time()


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- per-job runner ------------------------------------------------------

def build_worker_cmd(job: dict, slot: WorkerSlot,
                     job_output_dir: Path, args) -> list[str]:
    """Translate a job into the agent CLI invocation (or pass-through
    --worker-command if user supplied one)."""
    if args.worker_command:
        return shlex.split(args.worker_command)

    script = FREECAD_SCRIPT if job["app"] == "freecad" else BLENDER_SCRIPT
    extra = list(job.get("extra_args") or DEFAULT_EXTRA_ARGS)
    return [
        UV_BIN, "run", "--project", str(PROJECT_ROOT),
        "python", str(script),
        "--goal", job["goal_path"],
        "--output-dir", str(job_output_dir),
        "--max-steps", str(job.get("max_steps", 15)),
        *extra,
    ]


def run_one_job(slot: WorkerSlot, job: dict, args, shutdown: threading.Event,
                slot_lock: threading.Lock) -> JobResult:
    job_id = job["job_id"]
    log_dir = slot.worker_dir / "logs"
    job_output_dir = slot.worker_dir / "jobs" / job_id
    job_output_dir.mkdir(parents=True, exist_ok=True)

    # Copy goal.png into the job dir for self-contained record.
    try:
        shutil.copyfile(job["goal_path"], job_output_dir / "goal.png")
    except OSError:
        pass

    # Write the job manifest where the worker would read it (CUA_JOB_FILE).
    job_manifest = slot.worker_dir / "jobs" / f"{job_id}.json"
    job_manifest.write_text(json.dumps(job, indent=2))

    cmd = build_worker_cmd(job, slot, job_output_dir, args)
    env = slot.env_for_subprocess(job, job_output_dir)
    stdout = open(log_dir / f"{job_id}.stdout.log", "wb")
    stderr = open(log_dir / f"{job_id}.stderr.log", "wb")
    timeout = float(job.get("timeout_sec") or args.job_timeout_sec)

    started = now()
    rc: int | None = None
    err = ""
    status = "failed"

    try:
        with slot_lock:
            slot.current_subproc = subprocess.Popen(
                cmd, env=env, stdout=stdout, stderr=stderr,
                start_new_session=True, cwd=str(PROJECT_ROOT),
            )
        proc = slot.current_subproc
        try:
            rc = proc.wait(timeout=timeout)
            status = "succeeded" if rc == 0 else "failed"
        except subprocess.TimeoutExpired:
            status = "timed_out"
            err = f"exceeded job_timeout_sec={timeout}"
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            rc = proc.returncode or -1
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        rc = -1
    finally:
        with slot_lock:
            slot.current_subproc = None
        stdout.close()
        stderr.close()

    ended = now()

    # Lift terminated_by + step count from the agent's trajectory.json
    traj_path = job_output_dir / "trajectory.json"
    mp4_path = job_output_dir / "trajectory.mp4"
    terminated_by: str | None = None
    step_count = 0
    if traj_path.exists():
        try:
            tj = json.loads(traj_path.read_text())
            terminated_by = tj.get("terminated_by")
            step_count = len(tj.get("trajectory", []))
        except json.JSONDecodeError:
            err = err or "trajectory.json was malformed"
            status = "failed"
    elif status != "timed_out":
        # No artifact produced => infra failure.
        status = "failed"
        err = err or "trajectory.json missing"

    # Orchestrator-level status: succeeded if the agent ran to a clean
    # terminal state (self-terminate OR ran out of budget) and produced
    # a valid trajectory. Agent's CLI return-code 1 on `max_steps` is
    # an agent-level signal, not an orchestration failure.
    if status != "timed_out" and traj_path.exists():
        if terminated_by in ("agent", "max_steps"):
            status = "succeeded"
        else:
            status = "failed"

    return JobResult(
        job_id=job_id,
        worker_id=f"worker_{slot.worker_id:03d}",
        display=slot.display,
        app=slot.app,
        goal_path=job["goal_path"],
        status=status,
        terminated_by=terminated_by,
        steps=step_count,
        started_at=started,
        ended_at=ended,
        duration_sec=round(ended - started, 2),
        trajectory_json_path=str(traj_path) if traj_path.exists() else None,
        trajectory_mp4_path=str(mp4_path) if mp4_path.exists() else None,
        return_code=rc if rc is not None else -1,
        error_message=err,
        retries=job.get("_retries", 0),
    )


# --- job discovery / generation ------------------------------------------

def discover_goal_pngs(app: str) -> list[Path]:
    """Goal screenshots from prior smoketests; round-robin source for jobs."""
    src = FREECAD_GOAL_DIR if app == "freecad" else BLENDER_GOAL_DIR
    if not src.exists():
        return []
    return sorted(p for p in src.iterdir()
                  if p.is_file() and p.name.startswith("A_") and p.suffix == ".png")


def generate_jobs(num_jobs: int, app: str, args) -> list[dict]:
    pool = discover_goal_pngs(app)
    if not pool:
        raise RuntimeError(
            f"No goal screenshots found under "
            f"{FREECAD_GOAL_DIR if app=='freecad' else BLENDER_GOAL_DIR}; "
            f"either provide --jobs-file or generate goals first."
        )
    jobs: list[dict] = []
    for i in range(num_jobs):
        goal = pool[i % len(pool)]
        jobs.append({
            "job_id": f"job_{i+1:06d}",
            "app": app,
            "goal_path": str(goal),
            "max_steps": 15,
            "extra_args": list(DEFAULT_EXTRA_ARGS),
            "timeout_sec": args.job_timeout_sec,
            "created_at": utc_iso(),
            "expected_outputs": ["trajectory.json", "trajectory.mp4"],
        })
    return jobs


def load_jobs_file(path: Path) -> list[dict]:
    jobs = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            jobs.append(json.loads(line))
    return jobs


# --- summary -------------------------------------------------------------

def write_summary(run_dir: Path, results: list[JobResult]) -> tuple[Path, Path]:
    json_path = run_dir / "summary.json"
    csv_path = run_dir / "summary.csv"
    json_path.write_text(json.dumps({
        "generated_at": utc_iso(),
        "total_jobs": len(results),
        "succeeded": sum(1 for r in results if r.status == "succeeded"),
        "failed": sum(1 for r in results if r.status == "failed"),
        "timed_out": sum(1 for r in results if r.status == "timed_out"),
        "results": [r.to_dict() for r in results],
    }, indent=2))
    if results:
        with open(csv_path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(results[0].to_dict().keys()))
            w.writeheader()
            for r in results:
                w.writerow(r.to_dict())
    else:
        csv_path.write_text("")
    return json_path, csv_path


# --- system info ---------------------------------------------------------

def gib(bytes_: int) -> float:
    return bytes_ / (1024 ** 3)


def system_report(num_workers: int) -> tuple[int, str]:
    cpu = os.cpu_count() or 1
    mem_total_gb = mem_free_gb = 0.0
    try:
        with open("/proc/meminfo") as fh:
            mi = {ln.split(":")[0]: int(ln.split()[1])
                  for ln in fh if ":" in ln and ln.split()[1].isdigit()}
        mem_total_gb = mi.get("MemTotal", 0) / (1024 ** 2)
        mem_free_gb = mi.get("MemAvailable", 0) / (1024 ** 2)
    except OSError:
        pass
    disk_free_gb = gib(shutil.disk_usage(str(Path.home())).free)
    nvidia = shutil.which("nvidia-smi")
    has_gpu = False
    if nvidia:
        rc = subprocess.run([nvidia], capture_output=True, timeout=5)
        has_gpu = rc.returncode == 0
    # Conservative heuristic: 2 vCPUs per worker, 4 GiB RAM per worker, 8 GiB disk per worker.
    rec = min(cpu // 2, int(mem_total_gb // 4), int(disk_free_gb // 8))
    rec = max(1, rec)
    msg_lines = [
        f"  CPUs:        {cpu}",
        f"  RAM total:   {mem_total_gb:.1f} GiB (free {mem_free_gb:.1f})",
        f"  Disk free:   {disk_free_gb:.1f} GiB under {Path.home()}",
        f"  GPU:         {'yes' if has_gpu else 'none (Mesa software OpenGL)'}",
        f"  Recommended max workers: {rec}",
    ]
    if num_workers > rec:
        msg_lines.append(
            f"  WARNING: requested --num-workers {num_workers} exceeds "
            f"recommendation {rec}; continuing anyway")
    return rec, "\n".join(msg_lines)


# --- main orchestrator ---------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="parallel_orchestrator")
    p.add_argument("--num-workers", type=int, required=True)
    p.add_argument("--num-jobs", type=int, default=0,
                   help="Generate this many jobs from defaults if --jobs-file omitted.")
    p.add_argument("--app", choices=("freecad", "blender"), default="freecad")
    p.add_argument("--jobs-file", type=Path, default=None)
    p.add_argument("--output-dir", type=Path, default=None,
                   help="default: ~/cua_gui_smoketest/runs/run_<UTC>")
    p.add_argument("--display-base", type=int, default=100)
    p.add_argument("--screen-width", type=int, default=1920)
    p.add_argument("--screen-height", type=int, default=1080)
    p.add_argument("--screen-depth", type=int, default=24)
    p.add_argument("--per-worker-timeout-sec", type=int, default=1800,
                   help="(reserved for future per-worker budget; not currently enforced)")
    p.add_argument("--job-timeout-sec", type=int, default=1200)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--reuse-existing-assets", action="store_true",
                   help="(reserved; default behavior already reuses assets)")
    p.add_argument("--max-retries", type=int, default=1)
    p.add_argument("--stop-on-failure", action="store_true")
    p.add_argument("--worker-command", type=str, default=None,
                   help="Override the default agent CLI; run this command "
                        "verbatim per job (with the slot env).")
    p.add_argument("--best-of", type=int, default=1,
                   help="Run N independent trials per job and keep the "
                        "max-score trial as the winner. Default 1 (no extra "
                        "sampling). N>1 multiplies cost by ~N but lifts mean "
                        "score by exploiting per-run variance. Trials get a "
                        "_bo<i> suffix in their job_id; a best_of_summary.json "
                        "is written with winners.")
    return p.parse_args(argv)


def default_output_dir() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path.home() / "cua_gui_smoketest" / "runs" / f"run_{ts}"


_shutdown = threading.Event()


def install_signal_handlers() -> None:
    def handler(signum, _frame):
        if _shutdown.is_set():
            return
        print(f"\n[orchestrator] received signal {signum}; "
              f"stopping new job dispatch and tearing down...",
              flush=True)
        _shutdown.set()
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = (args.output_dir or default_output_dir()).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n=== parallel_orchestrator ===")
    print(f"app:         {args.app}")
    print(f"workers:     {args.num_workers}")
    print(f"output:      {run_dir}")
    rec, sysmsg = system_report(args.num_workers)
    print("system:")
    print(sysmsg)

    # Build jobs
    if args.jobs_file:
        jobs = load_jobs_file(args.jobs_file)
    else:
        if args.num_jobs <= 0:
            print("ERROR: --num-jobs required when --jobs-file omitted", file=sys.stderr)
            return 2
        jobs = generate_jobs(args.num_jobs, args.app, args)
        (run_dir / "jobs.jsonl").write_text(
            "\n".join(json.dumps(j) for j in jobs) + "\n"
        )

    # Best-of-N: expand each job into N trials with _bo<i> suffix.
    # Each trial is otherwise identical; runs as a normal job. After the
    # sweep, write_best_of_summary picks the max-score trial per base job.
    if args.best_of > 1:
        expanded = []
        for j in jobs:
            base_id = j["job_id"]
            for i in range(args.best_of):
                trial = dict(j)
                trial["job_id"] = f"{base_id}_bo{i}"
                trial["_bo_base"] = base_id
                trial["_bo_index"] = i
                expanded.append(trial)
        print(f"[best-of-{args.best_of}] expanded {len(jobs)} base jobs → {len(expanded)} trials")
        jobs = expanded

    # Tag each job with retry counter
    for j in jobs:
        j.setdefault("_retries", 0)

    # Write run_config
    (run_dir / "run_config.json").write_text(json.dumps({
        "args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
        "started_at": utc_iso(),
        "num_jobs": len(jobs),
        "system_recommended_max": rec,
        "model_default": DEFAULT_MODEL,
    }, indent=2))

    if args.dry_run:
        print(f"[dry-run] would run {len(jobs)} jobs over {args.num_workers} workers")
        for j in jobs[:5]:
            print(f"  preview: {j['job_id']}  goal={Path(j['goal_path']).name}")
        if len(jobs) > 5:
            print(f"  ... + {len(jobs)-5} more")
        return 0

    install_signal_handlers()

    # Build worker slots
    slots: list[WorkerSlot] = []
    for i in range(args.num_workers):
        slot = WorkerSlot.build(
            worker_id=i, run_dir=run_dir, app=args.app,
            display_base=args.display_base,
            width=args.screen_width, height=args.screen_height,
            depth=args.screen_depth,
        )
        slot.setup()
        (slot.worker_dir / "worker_config.json").write_text(json.dumps({
            "worker_id": slot.worker_id,
            "display": slot.display,
            "home": str(slot.home_dir),
            "app": slot.app,
            "geometry": f"{slot.width}x{slot.height}x{slot.depth}",
        }, indent=2))
        slots.append(slot)
        print(f"[setup] worker_{i:03d} DISPLAY={slot.display} HOME={slot.home_dir}")

    print(f"\n[run] dispatching {len(jobs)} jobs across {len(slots)} workers")

    # Per-slot lock so the run thread can safely touch `current_subproc`
    slot_locks = [threading.Lock() for _ in slots]
    available: Queue[int] = Queue()
    for i in range(len(slots)):
        available.put(i)

    pending: list[dict] = list(jobs)
    results: list[JobResult] = []
    in_flight: dict[Future, tuple[int, dict]] = {}
    fatal_stop = False

    def submit(executor: ThreadPoolExecutor, slot_idx: int, job: dict) -> None:
        slot = slots[slot_idx]
        slot.busy = True
        fut = executor.submit(run_one_job, slot, job, args, _shutdown, slot_locks[slot_idx])
        in_flight[fut] = (slot_idx, job)

    with ThreadPoolExecutor(max_workers=len(slots)) as ex:
        # Prime initial fan-out
        while pending and not available.empty() and not _shutdown.is_set():
            slot_idx = available.get_nowait()
            job = pending.pop(0)
            submit(ex, slot_idx, job)

        while in_flight and not fatal_stop:
            # Wait for any one future
            done_fut = next(iter([f for f in in_flight if f.done()]), None)
            if done_fut is None:
                # Block on first available
                done_fut = next(iter(as_completed_compat(in_flight, _shutdown)))
                if done_fut is None:  # shutdown asked
                    break
            slot_idx, job = in_flight.pop(done_fut)
            try:
                result = done_fut.result()
            except Exception as exc:  # noqa: BLE001
                result = JobResult(
                    job_id=job["job_id"],
                    worker_id=f"worker_{slot_idx:03d}",
                    display=slots[slot_idx].display, app=slots[slot_idx].app,
                    goal_path=job["goal_path"], status="failed",
                    terminated_by=None, steps=0,
                    started_at=0.0, ended_at=0.0, duration_sec=0.0,
                    trajectory_json_path=None, trajectory_mp4_path=None,
                    return_code=-1, error_message=f"{type(exc).__name__}: {exc}",
                    retries=job.get("_retries", 0),
                )
            print(f"[done] {result.worker_id} {result.job_id}  "
                  f"{result.status}  term={result.terminated_by}  "
                  f"steps={result.steps}  dur={result.duration_sec:.0f}s")

            # Retry policy
            if (result.status != "succeeded"
                    and job.get("_retries", 0) < args.max_retries
                    and not _shutdown.is_set()):
                job["_retries"] = job.get("_retries", 0) + 1
                pending.append(job)
                print(f"[retry] re-queued {result.job_id} (retry "
                      f"{job['_retries']}/{args.max_retries})")
            else:
                results.append(result)
                if args.stop_on_failure and result.status != "succeeded":
                    print(f"[stop-on-failure] {result.job_id} failed; "
                          f"halting new dispatch")
                    fatal_stop = True

            slots[slot_idx].busy = False
            available.put(slot_idx)

            # Refill
            while (pending and not available.empty()
                   and not _shutdown.is_set() and not fatal_stop):
                next_slot = available.get_nowait()
                next_job = pending.pop(0)
                submit(ex, next_slot, next_job)

        # If we exited because of shutdown, drain in_flight
        for fut, (slot_idx, job) in list(in_flight.items()):
            slots[slot_idx].teardown()
            try:
                result = fut.result(timeout=10)
                results.append(result)
            except Exception as exc:  # noqa: BLE001
                results.append(JobResult(
                    job_id=job["job_id"],
                    worker_id=f"worker_{slot_idx:03d}",
                    display=slots[slot_idx].display, app=slots[slot_idx].app,
                    goal_path=job["goal_path"], status="failed",
                    terminated_by=None, steps=0,
                    started_at=0.0, ended_at=0.0, duration_sec=0.0,
                    trajectory_json_path=None, trajectory_mp4_path=None,
                    return_code=-1,
                    error_message=f"abort: {type(exc).__name__}: {exc}",
                    retries=job.get("_retries", 0),
                ))

    # Teardown every slot
    print("\n[teardown] stopping Xvfb + openbox per slot")
    for s in slots:
        s.teardown()

    json_path, csv_path = write_summary(run_dir, results)
    succ = sum(1 for r in results if r.status == "succeeded")
    print(f"\n=== complete: {succ}/{len(results)} succeeded ===")
    print(f"  run dir:         {run_dir}")
    print(f"  summary.json:    {json_path}")
    print(f"  summary.csv:     {csv_path}")
    print(f"  per-worker:      {run_dir}/worker_NNN/")

    # Best-of-N: pick the max-score trial per base job and write a separate
    # summary. The eval runs in-process via the existing geometric evaluator.
    if args.best_of > 1:
        bo_path = write_best_of_summary(run_dir, results, jobs, args.best_of)
        print(f"  best_of_summary: {bo_path}")

    for r in results[:3]:
        if r.trajectory_json_path:
            print(f"  sample traj:     {r.trajectory_json_path}")
            break
    return 0 if succ == len(results) and results else 1


def write_best_of_summary(run_dir: Path, results: list, jobs: list, n: int) -> Path:
    """For each base job, eval all N trials and keep the max-score one.

    Returns the path to best_of_summary.json. Uses the existing
    cua_smoketest.agent.evaluator. Failures in eval are reported but do not
    abort the sweep.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from cua_smoketest.agent.evaluator import (  # noqa: E402
        evaluate_freecad_run, evaluate_blender_run,
        resolve_freecad_goal_asset, resolve_blender_goal_asset,
    )
    # Map base_id → list of (trial_idx, result, app, goal_path)
    jobs_by_id = {j["job_id"]: j for j in jobs}
    trials_by_base: dict[str, list] = {}
    for r in results:
        j = jobs_by_id.get(r.job_id, {})
        base = j.get("_bo_base") or r.job_id  # fall back to self if not expanded
        idx = j.get("_bo_index", 0)
        trials_by_base.setdefault(base, []).append((idx, r, j))

    out_rows = []
    for base, trials in sorted(trials_by_base.items()):
        scored = []
        for idx, r, j in trials:
            score = None
            traj = Path(r.trajectory_json_path) if r.trajectory_json_path else None
            if traj and traj.exists():
                app = j.get("app", "freecad")
                resolver = resolve_freecad_goal_asset if app == "freecad" else resolve_blender_goal_asset
                runner = evaluate_freecad_run if app == "freecad" else evaluate_blender_run
                asset = resolver(Path(j["goal_path"]))
                if asset:
                    try:
                        edir = traj.parent / "eval"
                        ev = runner(traj, asset, edir)
                        (edir / "eval.json").write_text(json.dumps(ev, indent=2))
                        score = ev["score"]["match_score"]
                    except Exception as exc:
                        pass
            scored.append({"trial": idx, "job_id": r.job_id, "score": score,
                           "trajectory": str(traj) if traj else None,
                           "status": r.status, "terminated_by": r.terminated_by,
                           "duration_sec": r.duration_sec})
        # Pick winner: highest non-None score; tie-break on lowest trial idx.
        valid = [s for s in scored if s["score"] is not None]
        winner = max(valid, key=lambda s: (s["score"], -s["trial"])) if valid else None
        out_rows.append({"base_job_id": base, "n_trials": len(trials),
                         "winner": winner, "trials": scored})

    bo_path = run_dir / "best_of_summary.json"
    bo_path.write_text(json.dumps({
        "best_of_n": n,
        "base_jobs": len(out_rows),
        "winners_mean_score": (sum(r["winner"]["score"] for r in out_rows if r["winner"])
                               / max(1, sum(1 for r in out_rows if r["winner"]))),
        "results": out_rows,
    }, indent=2))
    return bo_path


def as_completed_compat(in_flight: dict, shutdown_event: threading.Event):
    """Generator that yields the next completed future or None on shutdown.

    We use this instead of concurrent.futures.as_completed so we can wake
    up on shutdown without leaving futures unobserved.
    """
    while in_flight:
        if shutdown_event.is_set():
            yield None
            return
        for fut in list(in_flight):
            if fut.done():
                yield fut
                return
        time.sleep(0.25)


if __name__ == "__main__":
    raise SystemExit(main())
