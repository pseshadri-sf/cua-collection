"""Clone source repos for every board in a manifest so KIRECON_PROJECT_LIBS_ROOT
can point at the project-embedded footprints during reconstruct.

Profile of the 22 boards stuck at score=0 on the 50-board ablation (2026-06-12):
  - 18 boards reference a project-local .pretty or loose .kicad_mod file shipped
    in their source repo (otter, Mlab_*, footprints, custom, jSmartSW, etc.).
  - 3 boards have 100% lib coverage from the standard install yet still scored
    0 — those are planner failures, not library failures.
  - 1 board (Avionic-Mastodonte) has 96.6% coverage but exotic remainder.

For each board this script does:
  - skip if <cache>/<repo-slug> already exists
  - shallow clone (--depth=1) to <cache>/<repo-slug>
  - log success/failure to manifest.jsonl in <cache>

Run:
  uv run python scripts/kicad_clone_project_libs.py \
      --manifest ~/cua_kicad_smoketest/manifest.jsonl \
      --boards-list <path-or-NONE-for-all-broken> \
      --cache-root ~/kicad-project-libs
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _slug(repo_field: str) -> str:
    """Repo field is `<owner>/<name>`. Convert to a safe dir name."""
    return repo_field.replace("/", "__")


def _clone(url: str, dst: Path, timeout: int = 120) -> tuple[bool, str]:
    if dst.exists() and any(dst.iterdir()):
        return True, "already-cached"
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth=1", "--filter=blob:none",
           "--no-tags", "--single-branch", url, str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return True, "cloned"
        return False, f"git rc={r.returncode}: {r.stderr.strip()[:150]}"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _has_pretty(d: Path) -> int:
    return sum(1 for _ in d.rglob("*.pretty") if _.is_dir())


def _has_kicad_mod(d: Path) -> int:
    return sum(1 for _ in d.rglob("*.kicad_mod"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--cache-root", required=True, type=Path)
    ap.add_argument("--boards-list", type=Path, default=None,
                    help="JSON list of board basenames to limit cloning to. "
                         "If omitted, clones every board in the manifest.")
    ap.add_argument("--out-log", type=Path, default=None,
                    help="JSONL log of (board, slug, status, pretty_count, mod_count). "
                         "Defaults to <cache-root>/clone_log.jsonl")
    args = ap.parse_args(argv)

    if not args.cache_root.exists():
        args.cache_root.mkdir(parents=True)
    log_path = args.out_log or args.cache_root / "clone_log.jsonl"

    targets: list[str] | None = None
    if args.boards_list and args.boards_list.exists():
        targets = json.loads(args.boards_list.read_text())

    manifest = [json.loads(l) for l in args.manifest.read_text().splitlines() if l.strip()]
    log = open(log_path, "w")
    n_total = n_clone_ok = n_clone_fail = n_skip = 0
    for m in manifest:
        board = Path(m.get("staged_asset", "")).name
        if not board:
            continue
        if targets and board not in targets:
            continue
        repo = m.get("repo") or ""
        url = m.get("url") or ""
        if not repo or not url:
            n_skip += 1
            continue
        n_total += 1
        slug = _slug(repo)
        dst = args.cache_root / slug
        ok, msg = _clone(url, dst)
        pretty_count = _has_pretty(dst) if ok else 0
        mod_count = _has_kicad_mod(dst) if ok else 0
        rec = {"board": board, "repo": repo, "slug": slug, "ok": ok,
               "status": msg, "pretty_count": pretty_count, "mod_count": mod_count}
        log.write(json.dumps(rec) + "\n"); log.flush()
        if ok: n_clone_ok += 1
        else:  n_clone_fail += 1
        print(f"  [{('OK ' if ok else 'FAIL')}] {board[:42]:<44}  "
              f".pretty={pretty_count:>2}  .kicad_mod={mod_count:>4}  ({msg})", flush=True)
    log.close()
    print(f"\n  total={n_total}  cloned/cached={n_clone_ok}  failed={n_clone_fail}  skipped={n_skip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
