"""Local web app: FastAPI backend + static frontend. Listens on 127.0.0.1 only.

Run:  .venv/bin/python -m app.main
"""

from __future__ import annotations

import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import config, models, pipeline
from app.export import content_disposition, document_entries, folder_names, readme, stream_zip
from app.jobs import (JobStore, Scheduler, download_finished, download_started, result_dir,
                      safe_relative_path)
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
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


# ------------------------------------------------------------------------------------------ jobs
@app.post("/api/jobs")
async def create_job(request: Request):
    body = await request.json()
    try:
        return {"id": store.create_job(body.get("mode", "auto"))}
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
                    shutil.copyfileobj(part.file, fh, 1024 * 1024)
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
def get_markdown(fid: int):
    """Text of the final Markdown, for the in-page preview."""
    _, out = _done_file(fid)
    return PlainTextResponse(next(out.glob("*.md")).read_text(encoding="utf-8"))


@app.get("/api/files/{fid}/assets/{name}")
def get_asset(fid: int, name: str):
    """Images of one document, for the preview; confined to that document's assets folder."""
    _, out = _done_file(fid)
    base = (out / "assets").resolve()
    target = (base / name).resolve()
    if target.parent != base or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)


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
