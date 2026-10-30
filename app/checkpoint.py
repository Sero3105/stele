"""Per-page checkpoints so an interrupted conversion resumes where it stopped.

work/<job>/<file>/
    lock            flock: one process per file (a second server can't process the same pages)
    manifest.json   sha256 of the source + mode + pipeline version: checkpoints of another
                    file version or other parameters are discarded, never mixed
    pages/<n>.json  latest result of page n (+ engines already tried); written atomically

A page counts as done only once its JSON is fully on disk: writes go to a temp file, are
fsync'ed, then os.replace'd. Leftover temp files and unreadable JSON are ignored on load
(that page is simply converted again).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
from pathlib import Path

PIPELINE_VERSION = "3"


class Busy(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write(path: Path, data: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Checkpoint:
    def __init__(self, work_dir: Path, source: Path, mode: str):
        self.dir = work_dir
        self.pages_dir = work_dir / "pages"
        self.manifest = {"sha256": file_sha256(source), "mode": mode, "version": PIPELINE_VERSION}
        self._fd: int | None = None

    def __enter__(self) -> Checkpoint:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.dir / "lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._fd)
            self._fd = None
            raise Busy("file già in elaborazione da un altro processo") from None
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def load(self) -> dict[int, dict]:
        """Saved pages for this exact source/mode/version; anything else is wiped."""
        path = self.dir / "manifest.json"
        try:
            same = json.loads(path.read_text()) == self.manifest
        except (OSError, ValueError):
            same = False
        if not same:
            shutil.rmtree(self.pages_dir, ignore_errors=True)
            atomic_write(path, json.dumps(self.manifest))
        self.pages_dir.mkdir(exist_ok=True)
        pages = {}
        for f in self.pages_dir.iterdir():
            if f.name.startswith("."):
                f.unlink(missing_ok=True)  # temp file of an interrupted write
                continue
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                pages[int(rec["page"])] = rec
            except (OSError, ValueError, KeyError, TypeError):
                f.unlink(missing_ok=True)  # unreadable: redo that page
        return pages

    def save(self, rec: dict) -> None:
        atomic_write(self.pages_dir / f"{int(rec['page']):05d}.json", json.dumps(rec, ensure_ascii=False, default=str))

    def clear(self) -> None:
        shutil.rmtree(self.pages_dir, ignore_errors=True)
