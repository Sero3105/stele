"""Reliability: crash/restart recovery of a real server process, per-file failures that must not
stop the batch (engine error, memory, disk), upload limits and cancellation."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import httpx
import pytest

from app import config, pipeline
from app.jobs import JobStore, Scheduler

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "benchmarks" / "data"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(storage: Path, port: int) -> subprocess.Popen:
    env = os.environ | {"STELE_STORAGE": str(storage), "STELE_PORT": str(port)}
    proc = subprocess.Popen([sys.executable, "-m", "app.main"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        try:
            httpx.get(f"http://127.0.0.1:{port}/api/jobs", timeout=1)
            return proc
        except httpx.HTTPError:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("server did not start")


def marker_pdf(path: Path, pages: int = 48) -> Path:
    """Scanned PDF whose pages each carry a unique marker, to check order and duplicates."""
    sys.path.insert(0, str(ROOT / "benchmarks"))
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    import make_dataset as ds
    digital = path.with_suffix(".digital.pdf")
    c = canvas.Canvas(str(digital), pagesize=A4)
    for n in range(1, pages + 1):
        c.setFont("Helvetica-Bold", 16)
        c.drawString(72, 770, f"Marcatore pagina {n:03d}")
        c.setFont("Helvetica", 11)
        for k, sentence in enumerate(ds.LONG_SENTENCES[:6]):
            c.drawString(72, 730 - 18 * k, sentence[:90])
        c.showPage()
    c.save()
    ds.save_image_pdf(ds.rasterise(digital, 150), path, 150)
    return path


def test_kill_and_restart_resumes_from_interrupted_page(tmp_path):
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    storage = tmp_path / "data"
    pdf = marker_pdf(tmp_path / "marcatori.pdf")
    proc = _start(storage, port)
    try:
        job = httpx.post(f"{base}/api/jobs", json={"mode": "auto"}).json()["id"]
        files = [("files", ("marcatori.pdf", pdf.read_bytes())),
                 ("files", ("cartella/relazione.pdf", (DATA / "digital_it.pdf").read_bytes()))]
        assert len(httpx.post(f"{base}/api/jobs/{job}/files", files=files, timeout=60).json()["accepted"]) == 2
        deadline = time.time() + 120
        while time.time() < deadline:  # wait until some pages are saved but not all
            j = httpx.get(f"{base}/api/jobs/{job}").json()
            f = next(x for x in j["files"] if x["rel_path"] == "marcatori.pdf")
            if f["state"] == "running" and 8 <= f["pages_done"] < f["pages_total"]:
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"no partial progress observed: {j}")
        saved_before_kill = f["pages_done"]
        proc.send_signal(signal.SIGKILL)  # crash: no clean shutdown, possibly mid-write
        proc.wait()
        proc = _start(storage, port)
        deadline = time.time() + 300
        while time.time() < deadline:
            j = httpx.get(f"{base}/api/jobs/{job}").json()
            if all(x["state"] in ("done", "error") for x in j["files"]):
                break
            time.sleep(1)
        assert {x["state"] for x in j["files"]} == {"done"}, j["files"]
        fid = next(x["id"] for x in j["files"] if x["rel_path"] == "marcatori.pdf")
        md = httpx.get(f"{base}/api/files/{fid}/markdown").text
        positions = [md.find(f"Marcatore pagina {n:03d}") for n in range(1, 49)]
        assert all(p >= 0 for p in positions) and positions == sorted(positions)        # every page, in order
        assert all(md.count(f"Marcatore pagina {n:03d}") == 1 for n in range(1, 49))   # no duplicates
        report = json.loads((storage / "work" / job / str(fid) / "report.json").read_text())
        assert report["resumed_pages"] >= min(saved_before_kill, 8)  # resumed, not restarted from page 1
    finally:
        proc.kill()
        proc.wait()


def test_checkpoint_survives_interrupted_writes_and_rejects_other_versions(tmp_path):
    from app.checkpoint import Busy, Checkpoint
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"version 1")
    work = tmp_path / "work"
    with Checkpoint(work, src, "auto") as cp:
        cp.load()
        cp.save({"page": 1, "markdown": "uno"})
        cp.save({"page": 2, "markdown": "due"})
        (cp.pages_dir / ".00003.json.tmp").write_text('{"page": 3, "mark')   # crash during a write
        (cp.pages_dir / "00004.json").write_text('{"page": 4, "markdown": "qu')  # truncated file
        with pytest.raises(Busy):                                             # a second process
            with Checkpoint(work, src, "auto"):
                pass
    with Checkpoint(work, src, "auto") as cp:
        assert sorted(cp.load()) == [1, 2]                                    # only complete pages count
        assert sorted(p.name for p in cp.pages_dir.iterdir()) == ["00001.json", "00002.json"]
    with Checkpoint(work, src, "advanced") as cp:
        assert cp.load() == {}                                                # other parameters: discarded
    src.write_bytes(b"version 2")
    with Checkpoint(work, src, "advanced") as cp:
        assert cp.load() == {}                                                # other file content: discarded


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "db.sqlite3")


def _run_batch(store, names, monkeypatch, fail=None):
    """Queue dataset files and run the real scheduler; `fail(name)` may raise to simulate faults."""
    real = pipeline.convert_file

    def convert(src, ctx):
        if fail:
            fail(src.name)
        return real(src, ctx)
    monkeypatch.setattr(pipeline, "convert_file", convert)
    job = store.create_job("auto")
    for n in names:
        fid, dest = store.add_file(job, PurePosixPath(n), 0)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((DATA / n).read_bytes())
        store.mark_uploaded(fid, dest.stat().st_size)
    sched = Scheduler(store, workers=2)
    sched.start()
    deadline = time.time() + 300
    while time.time() < deadline:
        j = store.job(job)
        if not (j["counts"].get("queued") or j["counts"].get("running")):
            break
        time.sleep(0.3)
    sched.stop()
    return {f["rel_path"]: (f["state"], f["error"]) for f in j["files"]}


def test_engine_error_does_not_stop_batch(store, monkeypatch):
    def fail(name):
        if name == "digital_en_twocol.pdf":
            raise RuntimeError("model crashed")
    r = _run_batch(store, ["digital_it.pdf", "digital_en_twocol.pdf", "office_report_it.docx"], monkeypatch, fail)
    assert r["digital_en_twocol.pdf"] == ("error", "model crashed")
    assert r["digital_it.pdf"][0] == r["office_report_it.docx"][0] == "done"


def test_memory_exhaustion_is_reported(store, monkeypatch):
    def fail(name):
        if name == "digital_it.pdf":
            raise MemoryError("job needs ~25 GB, over the 20 GB budget")
    r = _run_batch(store, ["digital_it.pdf", "office_data.xlsx"], monkeypatch, fail)
    assert r["digital_it.pdf"][0] == "error" and r["digital_it.pdf"][1].startswith("Memoria insufficiente")
    assert r["office_data.xlsx"][0] == "done"


def test_disk_full_is_reported(store, monkeypatch):
    import shutil
    real = shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage", lambda p: real(p)._replace(free=config.MIN_FREE_DISK // 2))
    r = _run_batch(store, ["digital_it.pdf"], monkeypatch)
    assert r["digital_it.pdf"][0] == "error" and "Spazio su disco" in r["digital_it.pdf"][1]


def test_upload_limits_and_cancel(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    monkeypatch.setattr(config, "MAX_FILE_BYTES", 10_000)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        job = client.post("/api/jobs", json={"mode": "fast"}).json()["id"]
        client.post(f"/api/jobs/{job}/cancel")  # cancel before upload: nothing may start
        r = client.post(f"/api/jobs/{job}/files", files=[
            ("files", ("grande.pdf", (DATA / "scan_long_it.pdf").read_bytes())),
            ("files", ("piccolo.txt", b"ciao")),
        ]).json()
        assert [x["name"] for x in r["rejected"]] == ["grande.pdf"] and "troppo grande" in r["rejected"][0]["error"]
        time.sleep(3)
        j = client.get(f"/api/jobs/{job}").json()
        assert j["files"][0]["state"] == "queued" and j["cancelled"] == 1  # never picked up
        assert client.post("/api/jobs", json={"mode": "turbo"}).status_code == 400
        client.delete(f"/api/jobs/{job}")
        assert client.get(f"/api/jobs/{job}").status_code == 404
        assert not (config.UPLOADS / job).exists()


def test_cancel_while_waiting_for_memory_is_cancelled_not_error(store, monkeypatch):
    def fail(name):
        raise InterruptedError("cancelled while queued")  # what ResourceManager raises on cancel
    assert _run_batch(store, ["note.txt"], monkeypatch, fail)["note.txt"] == ("cancelled", None)


def test_cleanup_removes_folders_of_deleted_jobs(store):
    orphan, fresh = config.RESULTS / "deleted00000", config.RESULTS / "justcreated0"
    (orphan / "1").mkdir(parents=True)
    fresh.mkdir()
    os.utime(orphan, (time.time() - 7200,) * 2)
    Scheduler(store).cleanup()
    assert not orphan.exists() and fresh.exists()  # a brand-new job's folder is never touched
    fresh.rmdir()
