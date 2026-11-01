"""Local web app: FastAPI backend + static frontend. Listens on 127.0.0.1 only.

Run:  .venv/bin/python -m app.main
"""

from __future__ import annotations

import difflib
import io
import json
import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import config, models, pipeline
from app.analyzer import page_image
from app.export import content_disposition, document_entries, folder_names, readme, stream_zip
from app.jobs import (JobStore, Scheduler, download_finished, download_started, previous_dir, restore_previous,
                      result_dir, safe_relative_path)
from app.pipeline import safe_name
from app.resources import gpu_utilization, manager

log = logging.getLogger("app")
FRONTEND = config.ROOT / "frontend"
store = JobStore()
scheduler = Scheduler(store)


@asynccontextmanager
async def lifespan(_: FastAPI):
    manager()  # baseline footprint before any model loads
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="Stele", lifespan=lifespan)
# DNS rebinding: a web page whose domain later resolves to 127.0.0.1 reaches this server with its
# own Host header. Only local names are accepted, so other sites can't read the documents.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


# ------------------------------------------------------------------------------------------ jobs
@app.post("/api/jobs")
async def create_job(request: Request):
    body = await request.json()
    try:
        return {"id": store.create_job(body.get("mode", "auto"), bool(body.get("rag")), bool(body.get("split")),
                                       str(body.get("pages") or ""))}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/jobs/{job_id}/files")
async def upload(job_id: str, request: Request):
    """Multipart upload; each part's filename is its path relative to the selected folder."""
    if store.job(job_id) is None:
        raise HTTPException(404, "Lavoro non trovato")
    form = await request.form(max_files=5000, max_fields=5000)
    accepted, rejected = [], []
    try:
        for part in form.getlist("files"):
            if not hasattr(part, "filename"):
                continue
            try:
                rel = safe_relative_path(part.filename or "")
            except ValueError as exc:
                rejected.append({"name": part.filename, "error": str(exc)})
                continue
            size = part.size or 0
            if size > config.MAX_FILE_BYTES:
                rejected.append({"name": str(rel), "error": "File troppo grande (limite 1 GB)"})
                continue
            if shutil.disk_usage(config.STORAGE).free - size < config.MIN_FREE_DISK:
                rejected.append({"name": str(rel), "error": "Spazio su disco insufficiente"})
                continue
            fid, dest = store.add_file(job_id, rel, size)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open("wb") as fh:
                    await part.seek(0)
                    # off the event loop: a big file would freeze every other request meanwhile
                    await run_in_threadpool(shutil.copyfileobj, part.file, fh, 1024 * 1024)
                store.mark_uploaded(fid, dest.stat().st_size)
                accepted.append({"id": fid, "name": str(rel)})
            except OSError as exc:
                store.mark_upload_failed(fid, f"Salvataggio fallito: {exc}")
                rejected.append({"name": str(rel), "error": str(exc)})
    finally:
        await form.close()
    scheduler.notify()
    return {"accepted": accepted, "rejected": rejected}


@app.get("/api/jobs")
def list_jobs():
    return store.jobs()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = store.job(job_id)
    if job is None:
        raise HTTPException(404, "Lavoro non trovato")
    return job


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    store.cancel(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/keep")
async def keep_job(job_id: str, request: Request):
    """Keep (or stop keeping) a job past the retention period: a personal archive to search."""
    if store.job(job_id) is None:
        raise HTTPException(404, "Lavoro non trovato")
    store.keep(job_id, bool((await request.json()).get("keep")))
    return {"ok": True}


@app.post("/api/files/{fid}/retry")
async def retry_file(fid: int, request: Request):
    """Convert a file again from its upload: after an error, or in another mode ({"mode": ...})."""
    f = store.file(fid)
    if f is None:
        raise HTTPException(404, "File non trovato")
    body = await request.json() if await request.body() else {}
    _requeue(f, body.get("mode"))
    return {"ok": True}


def _requeue(f: dict, mode: str | None = None, recheck: dict | None = None) -> None:
    if not _upload(f).is_file():
        raise HTTPException(410, "Il file originale non è più disponibile: caricalo di nuovo")
    try:
        store.retry(f["id"], mode, recheck)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    scheduler.notify()


def _upload(f: dict) -> Path:
    return config.UPLOADS / f["job_id"] / Path(*PurePosixPath(f["rel_path"]).parts)


@app.post("/api/files/{fid}/feedback")
async def feedback(fid: int, request: Request):
    """The user's rating of a result: "good" keeps it; "soso" re-reads the pages to check (all
    pages if none) with a stronger engine; "improve" reconverts in Advanced mode, or, if it
    already was Advanced, re-reads every page with GLM-OCR. The replaced result is kept."""
    f, _ = _done_file(fid)
    rating = (await request.json()).get("rating")
    try:
        store.rate(fid, rating)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if rating == "good":
        return {"action": "none"}
    s = f["summary"] or {}
    if not s.get("pageable"):
        raise HTTPException(409, "Solo PDF e immagini si possono rileggere: il testo degli altri formati è già esatto")
    mode = store.q("SELECT COALESCE(f.mode, j.mode) m FROM files f JOIN jobs j ON j.id=f.job_id WHERE f.id=?",
                   (fid,))[0]["m"]
    if rating == "soso":
        _requeue(f, recheck={"pages": [p["page"] for p in s.get("review") or []], "engine": "next"})
        return {"action": "recheck"}
    if mode != "advanced":
        _requeue(f, mode="advanced")
        return {"action": "reconvert"}
    _requeue(f, recheck={"pages": [], "engine": "glm"})
    return {"action": "recheck"}


@app.get("/api/files/{fid}/compare")
def compare(fid: int):
    """What the last reconversion changed: a line diff of the two versions and their numbers."""
    f, out = _done_file(fid)
    prev = previous_dir(f["job_id"], fid)
    if not (prev / "summary.json").is_file():
        raise HTTPException(404, "Nessuna versione precedente")
    old_summary = json.loads((prev / "summary.json").read_text(encoding="utf-8"))

    def side(folder: Path, summary: dict) -> tuple[dict, list[str]]:
        text = pipeline.full_text(folder)
        meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        return {"mode": meta.get("mode"), "words": len(text.split()), "review": len(summary.get("review") or []),
                "converted_at": meta.get("converted_at")}, text.splitlines()
    (before, a), (after, b) = side(prev, old_summary), side(out, f["summary"] or {})
    diff = list(difflib.unified_diff(a, b, lineterm="", n=2))[2:5002]  # no file headers; bounded
    return {"previous": before, "current": after, "diff": diff}


@app.post("/api/files/{fid}/restore")
def restore(fid: int):
    """Go back to the previous version (the current one becomes the previous: undoable)."""
    _done_file(fid)
    try:
        restore_previous(store, fid)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.get("/api/search")
def search(q: str = ""):
    return store.search(q)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    store.delete(job_id)
    return {"ok": True}


# ------------------------------------------------------------------------------------------ results
def _done_file(fid: int) -> tuple[dict, Path]:
    f = store.file(fid)
    if f is None:
        raise HTTPException(404, "File non trovato")
    if f["state"] != "done":
        raise HTTPException(409, "Il documento non è stato convertito" if f["state"] in ("error", "cancelled")
                            else "Conversione ancora in corso")
    out = result_dir(f["job_id"], fid)
    if not any(out.glob("*.md")):
        raise HTTPException(404, "Risultato non disponibile")
    return f, out


@app.get("/api/files/{fid}/markdown")
def get_markdown(fid: int, full: bool = False):
    """Text of the final Markdown, for the in-page preview (`full`: chapter notes included)."""
    _, out = _done_file(fid)
    return PlainTextResponse(pipeline.full_text(out) if full else next(out.glob("*.md")).read_text(encoding="utf-8"))


@app.put("/api/files/{fid}/markdown")
async def save_markdown(fid: int, request: Request):
    """Hand-corrected text replaces the converted one (search index, CSVs and chunks follow)."""
    f, out = _done_file(fid)
    if (f["summary"] or {}).get("chapters"):
        raise HTTPException(409, "Documento diviso in capitoli: correggi le singole note nel vault")
    text = (await request.body()).decode("utf-8", errors="replace")
    if not text.strip():
        raise HTTPException(400, "Il documento non può essere vuoto")
    await run_in_threadpool(pipeline.save_edit, out, text)
    store.set_summary(fid, (f["summary"] or {}) | {"edited": True})
    store.index(fid, text)
    return {"ok": True}


@app.get("/api/files/{fid}/assets/{name}")
def get_asset(fid: int, name: str):
    """Images of one document, for the preview; confined to that document's assets folder."""
    _, out = _done_file(fid)
    base = (out / "assets").resolve()
    target = (base / name).resolve()
    if target.parent != base or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)


@app.get("/api/files/{fid}/pages/{n}")
def get_page(fid: int, n: int):
    """Original page n of a PDF or image, as PNG: shown next to the Markdown in the preview."""
    f, _ = _done_file(fid)
    src = _upload(f)
    try:
        im = page_image(src, n, dpi=110)
    except (OSError, ValueError, IndexError) as exc:
        raise HTTPException(404, "Pagina non disponibile") from exc
    buf = io.BytesIO()
    im.save(buf, "PNG")
    im.close()
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


def _zip_response(job_id: str, entries: list, filename: str) -> StreamingResponse:
    download_started(job_id)

    def body():
        try:
            yield from stream_zip(entries)
        finally:
            download_finished(job_id)
    return StreamingResponse(body(), media_type="application/zip",
                             headers={"Content-Disposition": content_disposition(filename),
                                      "Cache-Control": "no-store"})


@app.get("/api/files/{fid}/zip")
def file_zip(fid: int):
    f, out = _done_file(fid)
    folder = safe_name(PurePosixPath(f["rel_path"]).stem)
    return _zip_response(f["job_id"], document_entries(out, folder), f"{folder}.zip")


@app.get("/api/jobs/{job_id}/zip")
def job_zip(job_id: str):
    job = store.job(job_id)
    if job is None:
        raise HTTPException(404, "Lavoro non trovato")
    c = job["counts"]
    if c.get("queued") or c.get("running") or c.get("uploading"):
        raise HTTPException(409, "Conversione ancora in corso: lo ZIP sarà disponibile al termine")
    done = [f for f in job["files"] if f["state"] == "done"]
    if not done:
        raise HTTPException(409, "Nessun documento è stato convertito: non c'è nulla da scaricare")
    folders = folder_names(done)
    if len(done) == 1:
        f = done[0]
        stem = folders[f["id"]].rsplit("/", 1)[-1]
        return _zip_response(job_id, document_entries(result_dir(job_id, f["id"]), stem), f"{stem}.zip")
    entries: list = [("README.txt", readme(job, folders))]
    for f in done:
        entries += document_entries(result_dir(job_id, f["id"]), folders[f["id"]])
    return _zip_response(job_id, entries, "conversione.zip")


# ------------------------------------------------------------------------------------------ system
@app.get("/api/system")
def system():
    snap = manager().snapshot()
    engines = {}
    for key, eng in pipeline.ENGINES.items():
        ok, why = eng.available()
        engines[key] = {"name": eng.name, "available": ok, "detail": why,
                        "model_mb": eng.resources.model_memory_mb, "max_parallel": eng.resources.max_parallel_jobs}
    return snap | {"cpu_percent": psutil.cpu_percent(interval=None), "cpu_count": psutil.cpu_count(),
                   "gpu_percent": gpu_utilization(),  # None -> "Non disponibile"
                   "queue": store.queue_counts(), "engines": engines, "workers": scheduler.workers}


@app.get("/api/models")
def model_status():
    return models.status()


@app.exception_handler(MemoryError)
def memory_error(_, exc: MemoryError):
    return JSONResponse({"detail": str(exc)}, status_code=507)


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("docling").setLevel(logging.WARNING)  # per-page INFO chatter drowns the app log
    print(f"Apri http://{config.HOST}:{config.PORT}")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
