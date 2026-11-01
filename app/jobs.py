"""Job store (SQLite) and batch scheduler.

A job is one upload session (files/folders + mode); each file is converted independently,
so one failure never stops the batch. Worker threads pull queued files FIFO within a job and
round-robin across jobs, so a single file is not stuck behind a big folder; how many
actually run at once is decided by the ResourceManager leases inside the engines, so the
thread count is only an upper bound. State lives in SQLite: after a restart, files that
were running are re-queued and converted again.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import traceback
import uuid
from pathlib import Path, PurePosixPath

from app import config, markdown, pipeline, router
from app.checkpoint import PIPELINE_VERSION, Busy, atomic_write, file_sha256
from app.quality import strip_markdown

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, created REAL, mode TEXT, cancelled INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, rel_path TEXT, size INTEGER,
    state TEXT, stage TEXT DEFAULT '', pages_done INTEGER DEFAULT 0, pages_total INTEGER DEFAULT 0,
    engines TEXT DEFAULT '', summary TEXT, error TEXT, created REAL, started REAL, finished REAL
);
CREATE INDEX IF NOT EXISTS files_state ON files(state, id);
CREATE INDEX IF NOT EXISTS files_job ON files(job_id, state);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(text, file_id UNINDEXED, tokenize='unicode61 remove_diacritics 2');
"""
# Columns added after the first release: created on old databases too.
COLUMNS = [("jobs", "rag", "INTEGER DEFAULT 0"),     # page markers + chunks.jsonl
           ("jobs", "keep", "INTEGER DEFAULT 0"),    # never deleted by the retention cleanup
           ("jobs", "split", "INTEGER DEFAULT 0"),   # one note per chapter
           ("jobs", "pages", "TEXT DEFAULT ''"),     # PDFs: only these pages ("1-5, 8")
           ("files", "mode", "TEXT"),                # set when a file is reconverted in another mode
           ("files", "source_key", "TEXT"),          # sha256:mode:options:pipeline version of a done file
           ("files", "feedback", "TEXT"),            # the user's rating of the current result
           ("files", "recheck", "TEXT")]             # JSON: pages to re-read with a stronger engine
# Linked images live outside .md/.html files; an e-mail's attachments become files of the job: never reuse.
REUSE_SKIP = {".md", ".html", ".htm", ".eml"}
RATINGS = ("good", "soso", "improve")


def safe_relative_path(name: str) -> PurePosixPath:
    """Relative upload path; rejects absolute paths, traversal and empty parts."""
    name = name.replace("\\", "/").strip()
    raw_parts = name.split("/")  # check before PurePosixPath silently normalises "a//b" and "a/./b"
    if not name or name.startswith("/") or any(p in ("", ".", "..") for p in raw_parts):
        raise ValueError(f"Nome file non valido: {name!r}")
    if any(p.startswith(".") for p in raw_parts):
        raise ValueError(f"File nascosto ignorato: {name!r}")
    return PurePosixPath(name)


class JobStore:
    def __init__(self, db_path: Path = config.DB_PATH):
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        for table, col, decl in COLUMNS:
            if col not in {r[1] for r in self._db.execute(f"PRAGMA table_info({table})")}:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self._db.execute("CREATE INDEX IF NOT EXISTS files_key ON files(source_key)")
        self._lock = threading.Lock()

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._db.execute(sql, args).lastrowid

    # ---------------------------------------------------------------- jobs/files
    def create_job(self, mode: str) -> str:
        if mode not in router.MODES:
            raise ValueError(f"Modalità sconosciuta: {mode}")
        job_id = uuid.uuid4().hex[:12]
        self.x("INSERT INTO jobs(id, created, mode) VALUES (?,?,?)", (job_id, time.time(), mode))
        return job_id

    def add_file(self, job_id: str, rel: PurePosixPath, size: int) -> tuple[int, Path]:
        """Register an upload; returns (file id, destination path). Same name twice -> 'x (2).pdf'."""
        if not self.q("SELECT 1 FROM jobs WHERE id=?", (job_id,)):
            raise KeyError(job_id)
        taken = {r["rel_path"] for r in self.q("SELECT rel_path FROM files WHERE job_id=?", (job_id,))}
        candidate, i = rel, 2
        while str(candidate) in taken:
            candidate = rel.with_name(f"{rel.stem} ({i}){rel.suffix}")
            i += 1
        fid = self.x("INSERT INTO files(job_id, rel_path, size, state, created) VALUES (?,?,?,?,?)",
                     (job_id, str(candidate), size, "uploading", time.time()))
        return fid, config.UPLOADS / job_id / Path(*candidate.parts)

    def mark_uploaded(self, fid: int, size: int) -> None:
        self.x("UPDATE files SET state='queued', size=? WHERE id=?", (size, fid))

    def mark_upload_failed(self, fid: int, error: str) -> None:
        self.x("UPDATE files SET state='error', error=?, finished=? WHERE id=?", (error, time.time(), fid))

    def job(self, job_id: str) -> dict | None:
        rows = self.q("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not rows:
            return None
        files = [self._file_dict(r) for r in self.q("SELECT * FROM files WHERE job_id=? ORDER BY id", (job_id,))]
        counts: dict[str, int] = {}
        for f in files:
            counts[f["state"]] = counts.get(f["state"], 0) + 1
        return dict(rows[0]) | {"files": files, "counts": counts}

    def jobs(self) -> list[dict]:
        out = []
        for r in self.q("SELECT * FROM jobs ORDER BY created DESC LIMIT 50"):
            counts = {c["state"]: c["n"] for c in self.q(
                "SELECT state, COUNT(*) n FROM files WHERE job_id=? GROUP BY state", (r["id"],))}
            out.append(dict(r) | {"counts": counts})
        return out

    def file(self, fid: int) -> dict | None:
        rows = self.q("SELECT * FROM files WHERE id=?", (fid,))
        return self._file_dict(rows[0]) if rows else None

    @staticmethod
    def _file_dict(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["summary"] = json.loads(d["summary"]) if d["summary"] else None
        now = d["finished"] or time.time()
        d["elapsed"] = round(now - d["started"], 1) if d["started"] else None
        return d

    def queue_counts(self) -> dict[str, int]:
        return {r["state"]: r["n"] for r in self.q("SELECT state, COUNT(*) n FROM files GROUP BY state")}

    def cancel(self, job_id: str) -> None:
        self.x("UPDATE jobs SET cancelled=1 WHERE id=?", (job_id,))
        self.x("UPDATE files SET state='cancelled', finished=? WHERE job_id=? AND state IN ('queued','uploading')",
               (time.time(), job_id))

    def retry(self, fid: int, mode: str | None = None, recheck: dict | None = None) -> None:
        """Queue a finished, failed or cancelled file again, optionally in another mode or with
        some pages re-read by a stronger engine (`recheck`, see pipeline.Context)."""
        f = self.file(fid)
        if f is None:
            raise KeyError(fid)
        if f["state"] not in ("done", "error", "cancelled"):
            raise ValueError("Il documento è ancora in conversione")
        if mode is not None and mode not in router.MODES:
            raise ValueError(f"Modalità sconosciuta: {mode}")
        self.x("UPDATE jobs SET cancelled=0 WHERE id=?", (f["job_id"],))
        # The summary stays: it describes the result kept as the previous version.
        self.x("UPDATE files SET state='queued', mode=COALESCE(?, mode), recheck=?, stage='', error=NULL, "
               "pages_done=0, pages_total=0, started=NULL, finished=NULL WHERE id=?",
               (mode, json.dumps(recheck) if recheck else None, fid))

    def rate(self, fid: int, rating: str) -> None:
        if rating not in RATINGS:
            raise ValueError(f"Giudizio sconosciuto: {rating}")
        self.x("UPDATE files SET feedback=? WHERE id=?", (rating, fid))

    def set_summary(self, fid: int, summary: dict) -> None:
        self.x("UPDATE files SET summary=? WHERE id=?", (json.dumps(summary, default=str), fid))

    def finish(self, fid: int, summary: dict, source_key: str | None, text: str) -> None:
        self.x("UPDATE files SET state='done', stage='', summary=?, engines=?, finished=?, source_key=?, "
               "recheck=NULL, feedback=NULL, error=NULL, pages_done=pages_total WHERE id=?",
               (json.dumps(summary, default=str), ", ".join(summary["engines"]), time.time(), source_key, fid))
        self.index(fid, text)

    def reusable(self, source_key: str, fid: int) -> sqlite3.Row | None:
        rows = self.q("SELECT id, job_id, summary FROM files WHERE source_key=? AND state='done' AND id!=? "
                      "ORDER BY finished DESC LIMIT 1", (source_key, fid))
        return rows[0] if rows else None

    def index(self, fid: int, markdown: str) -> None:
        self.x("DELETE FROM search WHERE file_id=?", (fid,))
        self.x("INSERT INTO search(text, file_id) VALUES (?,?)", (strip_markdown(markdown), fid))

    def delete(self, job_id: str) -> None:
        self.cancel(job_id)
        self.x("DELETE FROM files WHERE job_id=?", (job_id,))
        self.x("DELETE FROM jobs WHERE id=?", (job_id,))
        for root in (config.UPLOADS, config.WORK, config.RESULTS):
            shutil.rmtree(root / job_id, ignore_errors=True)

    def expired(self, max_age_s: float) -> list[str]:
        """Jobs older than max_age_s with nothing queued or running."""
        return [r["id"] for r in self.q(
            "SELECT j.id FROM jobs j WHERE j.created < ? AND NOT EXISTS (SELECT 1 FROM files f "
            "WHERE f.job_id=j.id AND f.state IN ('queued','running','uploading'))", (time.time() - max_age_s,))]

    def is_cancelled(self, job_id: str) -> bool:
        rows = self.q("SELECT cancelled FROM jobs WHERE id=?", (job_id,))
        return not rows or bool(rows[0]["cancelled"])

    def recover(self) -> int:
        """After a restart: interrupted conversions go back to the queue, half uploads are dropped."""
        n = len(self.q("SELECT id FROM files WHERE state='running'"))
        self.x("UPDATE files SET state='queued', stage='ripreso dopo il riavvio', started=NULL WHERE state='running'")
        self.x("UPDATE files SET state='error', error='Caricamento interrotto' WHERE state='uploading'")
        return n

    def claim(self) -> sqlite3.Row | None:
        """Next file: the oldest queued one of the job with the fewest files running, so a file
        uploaded after a 25-file folder starts at the next free worker instead of waiting for all 25."""
        with self._lock:
            row = self._db.execute(
                "SELECT f.*, j.mode FROM files f JOIN jobs j ON j.id=f.job_id "
                "WHERE f.id IN (SELECT MIN(id) FROM files WHERE state='queued' GROUP BY job_id) AND j.cancelled=0 "
                "ORDER BY (SELECT COUNT(*) FROM files r WHERE r.job_id=f.job_id AND r.state='running'), f.id "
                "LIMIT 1").fetchone()
            if row:
                self._db.execute("UPDATE files SET state='running', started=?, stage='avvio' WHERE id=?",
                                 (time.time(), row["id"]))
            return row


def result_dir(job_id: str, fid: int) -> Path:
    """Final document folder of one file: <stem>.md, assets/, metadata.json."""
    return config.RESULTS / job_id / str(fid)


# Jobs whose ZIP is being streamed right now: the retention cleanup must not delete them.
_downloads: dict[str, int] = {}
_downloads_lock = threading.Lock()


def download_started(job_id: str) -> None:
    with _downloads_lock:
        _downloads[job_id] = _downloads.get(job_id, 0) + 1


def download_finished(job_id: str) -> None:
    with _downloads_lock:
        _downloads[job_id] -= 1
        if not _downloads[job_id]:
            del _downloads[job_id]


def downloading(job_id: str) -> bool:
    with _downloads_lock:
        return job_id in _downloads


class Scheduler:
    def __init__(self, store: JobStore, workers: int | None = None):
        self.store = store
        self.workers = workers or int(os.environ.get("STELE_WORKERS", "4"))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self._stop.clear()  # restartable: the server lifespan (and each TestClient) calls start/stop
        recovered = self.store.recover()
        if recovered:
            log.info("re-queued %d interrupted conversions", recovered)
        for i in range(self.workers):
            t = threading.Thread(target=self._loop, name=f"worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        threading.Thread(target=self._janitor, name="retention", daemon=True).start()

    def cleanup(self) -> list[str]:
        """Delete jobs past the retention period (never active ones or ones being downloaded),
        and stray page renders older than a day in the temp area."""
        removed = []
        for job_id in self.store.expired(config.RETENTION_DAYS * 86400):
            if not downloading(job_id):
                self.store.delete(job_id)
                removed.append(job_id)
        # Folders of deleted jobs: a conversion still running at delete time can publish afterwards.
        known = {r["id"] for r in self.store.q("SELECT id FROM jobs")}
        for root in (config.UPLOADS, config.WORK, config.RESULTS):
            for d in root.iterdir():
                # age check: a job created after the query above already has its folders
                if d.is_dir() and d.name not in known and time.time() - d.stat().st_mtime > 3600:
                    shutil.rmtree(d, ignore_errors=True)
        for d in config.TEMP.glob("tmp*"):
            if d.is_dir() and time.time() - d.stat().st_mtime > 86400:
                shutil.rmtree(d, ignore_errors=True)
        return removed

    def _janitor(self) -> None:
        while True:
            try:
                if removed := self.cleanup():
                    log.info("retention: removed %d expired jobs", len(removed))
            except Exception:  # cleanup must never take the scheduler down
                log.exception("retention cleanup failed")
            if self._stop.wait(3600):
                return

    def notify(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            row = self.store.claim()
            if row is None:
                self._wake.wait(timeout=2)
                self._wake.clear()
                continue
            self._run(row)

    def _run(self, row) -> None:
        fid, job_id, rel = row["id"], row["job_id"], PurePosixPath(row["rel_path"])
        src = config.UPLOADS / job_id / Path(*rel.parts)
        last = [0.0]

        def progress(info: dict) -> None:
            now = time.time()
            if now - last[0] < 0.5 and info.get("stage") not in ("completato", "analisi"):
                return
            last[0] = now
            sets, args = ["stage=?"], [info.get("stage", "")]
            for k in ("pages_done", "pages_total"):
                if k in info:
                    sets.append(f"{k}=?")
                    args.append(info[k])
            self.store.x(f"UPDATE files SET {', '.join(sets)} WHERE id=?", (*args, fid))

        ctx = pipeline.Context(out_dir=result_dir(job_id, fid), work_dir=config.WORK / job_id / str(fid),
                               name=rel.name, mode=row["mode"], upload_root=config.UPLOADS / job_id, progress=progress,
                               cancelled=lambda: self.store.is_cancelled(job_id))
        try:
            key = None
            if rel.suffix.lower() not in REUSE_SKIP and not ctx.split and not ctx.recheck and src.is_file():
                key = f"{file_sha256(src)}:{ctx.mode}:{int(ctx.rag)}:{ctx.pages}:{PIPELINE_VERSION}"
            prev = self.store.reusable(key, fid) if key else None
            summary = reuse(prev, ctx) if prev else None
            if summary is None:
                summary = pipeline.convert_file(src, ctx)
                summary.pop("report", None)  # per-page internals stay in the work dir
            self.store.finish(fid, summary, key, pipeline.full_text(ctx.out_dir))
        except Busy as exc:  # another process holds this file's lock: leave it to that process
            log.warning("%s: %s", rel, exc)
            self.store.x("UPDATE files SET state='error', error=? WHERE id=?", (str(exc), fid))
        except (pipeline.Cancelled, InterruptedError):  # InterruptedError: cancelled while waiting for memory
            self.store.x("UPDATE files SET state='cancelled', finished=? WHERE id=?", (time.time(), fid))
        except Exception as exc:  # any per-file failure is recorded, the batch continues
            log.warning("conversion failed for %s: %s", rel, exc)
            log.debug(traceback.format_exc())
            msg = str(exc) or type(exc).__name__
            if isinstance(exc, MemoryError):
                msg = f"Memoria insufficiente: {msg}"
            self.store.x("UPDATE files SET state='error', error=?, engines=?, finished=? WHERE id=?",
                         (msg[:2000], ", ".join(sorted(ctx.engines_used)), time.time(), fid))


def reuse(prev: sqlite3.Row, ctx: pipeline.Context) -> dict | None:
    """Copy the published result of an identical, already converted file (renamed after this
    file), or None if it can't be used (e.g. deleted meanwhile): then the file is converted."""
    try:
        src = result_dir(prev["job_id"], prev["id"])
        old_md = next(src.glob("*.md"))
        stage = ctx.work_dir / "final"
        shutil.rmtree(stage, ignore_errors=True)
        shutil.copytree(src, stage)
        md = stage / f"{ctx.stem}.md"
        if old_md.name != md.name:
            (stage / old_md.name).rename(md)
        meta = json.loads((stage / "metadata.json").read_text(encoding="utf-8"))
        atomic_write(stage / "metadata.json", json.dumps(meta | {"source": ctx.name}, ensure_ascii=False, indent=2))
        chunks = stage / "chunks.jsonl"
        if chunks.exists():
            rows = [json.loads(line) for line in chunks.read_text(encoding="utf-8").splitlines() if line]
            for i, r in enumerate(rows, 1):
                r.update(id=f"{ctx.stem}#{i}", source=ctx.name)
            atomic_write(chunks, markdown.jsonl(rows))
        if ctx.out_dir.exists():
            shutil.rmtree(ctx.out_dir)
        ctx.out_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage, ctx.out_dir)
    except (OSError, ValueError, StopIteration) as exc:
        log.info("result of file %s not reusable (%s): converting", prev["id"], exc)
        return None
    summary = json.loads(prev["summary"])
    return summary | {"markdown": str(ctx.out_dir / md.name), "reused": True, "seconds": 0, "resumed_pages": 0}


