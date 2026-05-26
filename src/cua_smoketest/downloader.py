from __future__ import annotations

import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class _LibraryFile:
    path: str            # repo-relative
    size: int            # bytes
    ext: str             # lower-case, leading dot

    @property
    def parent(self) -> str:
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""

    @property
    def basename(self) -> str:
        return self.path.rsplit("/", 1)[-1]


class AssetDownloader:
    """Downloads small CAD samples from the FreeCAD-library public repo.

    Strategy:
      * Partial shallow clone (--filter=blob:none --depth=1) into cache_dir
        so we only fetch tree + size info, not blobs.
      * Use `git ls-tree -r -l HEAD` to inspect every blob's size offline.
      * Pick `count` files biased toward variety (one per parent dir,
        preferred extensions first, deterministic ordering).
      * `git checkout` each pick (this materialises just that blob), then
        copy into target_dir with a sanitised, collision-free filename.
    """

    REPO_URL = "https://github.com/FreeCAD/FreeCAD-library.git"
    # Order = preference (loaded most reliably by FreeCAD GUI from a CLI arg).
    EXT_PREFERENCE: tuple[str, ...] = (".step", ".stp", ".FCStd", ".stl", ".iges", ".igs", ".brep")
    _EXTS = set(e.lower() for e in EXT_PREFERENCE)

    def __init__(self, target_dir: Path, cache_dir: Path, logs_dir: Path):
        self.target_dir = target_dir
        self.cache_dir = cache_dir
        self.logs_dir = logs_dir

    # --- public API --------------------------------------------------------

    def download(self, count: int,
                 exclude_basenames: set[str] | None = None,
                 max_file_size: int = 262_144,
                 min_file_size: int = 1_500) -> list[Path]:
        """Download `count` files. `exclude_basenames` should contain the
        target-directory filenames (i.e. the parent-prefixed safe names that
        this class would produce) so a prior run's picks are skipped."""
        if shutil.which("git") is None:
            raise RuntimeError("git not installed; cannot fetch FreeCAD-library")
        self.target_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_clone()

        catalog = self._catalog()
        picks = self._pick(catalog, count, exclude_basenames or set(),
                           min_file_size, max_file_size)
        copied: list[Path] = []
        for lf in picks:
            try:
                self._checkout(lf.path)
            except subprocess.CalledProcessError:
                continue
            src = self.cache_dir / lf.path
            if not src.exists() or src.stat().st_size == 0:
                continue
            dest = self.target_dir / self._safe_name(lf)
            shutil.copyfile(src, dest)
            copied.append(dest)
        return copied

    # --- internals ---------------------------------------------------------

    def _ensure_clone(self) -> None:
        if (self.cache_dir / ".git").exists():
            return
        self.cache_dir.parent.mkdir(parents=True, exist_ok=True)
        log = self.logs_dir / "downloader_clone.log"
        with open(log, "ab") as out:
            subprocess.run(
                ["git", "clone", "--filter=blob:none", "--depth=1",
                 "--no-checkout", self.REPO_URL, str(self.cache_dir)],
                stdout=out, stderr=subprocess.STDOUT, check=True, timeout=600,
            )

    def _catalog(self) -> list[_LibraryFile]:
        result = subprocess.run(
            ["git", "-C", str(self.cache_dir), "ls-tree", "-r", "-l", "HEAD"],
            capture_output=True, text=True, check=True, timeout=60,
        )
        files: list[_LibraryFile] = []
        # Format: <mode> <type> <hash> <size>\t<path>
        line_re = re.compile(r"^\S+\s+blob\s+\S+\s+(\d+)\t(.+)$")
        for line in result.stdout.splitlines():
            m = line_re.match(line)
            if not m:
                continue
            size = int(m.group(1))
            path = m.group(2)
            ext = ("." + path.rsplit(".", 1)[-1]).lower() if "." in path else ""
            if ext not in self._EXTS:
                continue
            files.append(_LibraryFile(path=path, size=size, ext=ext))
        return files

    def _pick(self, catalog: list[_LibraryFile], count: int,
              exclude_target_names: set[str],
              min_size: int, max_size: int) -> list[_LibraryFile]:
        # Filter by size + exclusion list (matched against the *target-dir*
        # filename this class would produce, not the repo basename).
        viable = [
            lf for lf in catalog
            if min_size <= lf.size <= max_size
            and self._safe_name(lf) not in exclude_target_names
            # Skip paths with characters that confuse FreeCAD's CLI arg parser.
            and "\\" not in lf.path and "\n" not in lf.path
        ]
        # Group by parent dir for variety.
        by_parent: dict[str, list[_LibraryFile]] = defaultdict(list)
        for lf in viable:
            by_parent[lf.parent].append(lf)

        # Within each parent: prefer extension order, then ascending size.
        ext_rank = {e.lower(): i for i, e in enumerate(self.EXT_PREFERENCE)}
        for parent, items in by_parent.items():
            items.sort(key=lambda f: (ext_rank.get(f.ext, 99), f.size, f.path))

        # Round-robin pick one per parent, deterministic parent order.
        parents = sorted(by_parent)
        picks: list[_LibraryFile] = []
        cursors = {p: 0 for p in parents}
        while len(picks) < count:
            progress = False
            for p in parents:
                if len(picks) >= count:
                    break
                if cursors[p] < len(by_parent[p]):
                    picks.append(by_parent[p][cursors[p]])
                    cursors[p] += 1
                    progress = True
            if not progress:
                break
        return picks

    def _checkout(self, repo_path: str) -> None:
        log = self.logs_dir / "downloader_checkout.log"
        with open(log, "ab") as out:
            subprocess.run(
                ["git", "-C", str(self.cache_dir), "checkout", "HEAD", "--", repo_path],
                stdout=out, stderr=subprocess.STDOUT, check=True, timeout=120,
            )

    @staticmethod
    def _safe_name(lf: _LibraryFile) -> str:
        # Encode the parent dir into the filename so two files with the same
        # basename from different dirs don't collide in target_dir.
        parent_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", lf.parent).strip("-")
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", lf.basename).strip("-")
        return f"{parent_slug}__{base}" if parent_slug else base

    @staticmethod
    def _safe_name_for_basename(basename: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "-", basename).strip("-")
