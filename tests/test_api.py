"""End-to-end through the HTTP API: uploads (files, folders, duplicates), conversion, streamed
ZIP downloads (layout, headers, no intermediates, concurrency), failures and cleanup."""

import io
import json
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config

DATA = Path(__file__).resolve().parent.parent / "benchmarks" / "data"


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


def run_job(client, files: dict[str, str | bytes], mode="auto") -> dict:
    job = client.post("/api/jobs", json={"mode": mode}).json()["id"]
    parts = [("files", (rel, src if isinstance(src, bytes) else (DATA / src).read_bytes())) for rel, src in files.items()]
    r = client.post(f"/api/jobs/{job}/files", files=parts).json()
    assert len(r["accepted"]) == len(files), r
    deadline = time.time() + 600
    while time.time() < deadline:
        j = client.get(f"/api/jobs/{job}").json()
        if not (j["counts"].get("queued") or j["counts"].get("running")):
            return j
        time.sleep(0.5)
    raise TimeoutError(job)


def zip_of(resp) -> zipfile.ZipFile:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(resp.content))


def test_single_document_zip(client):
    j = run_job(client, {"relazione.pdf": "digital_it.pdf"})
    r = client.get(f"/api/jobs/{j['id']}/zip")
    z = zip_of(r)
    assert r.headers["content-disposition"] == 'attachment; filename="relazione.zip"; filename*=UTF-8\'\'relazione.zip'
    assert sorted(z.namelist()) == ["relazione/assets/immagine_001.png", "relazione/metadata.json", "relazione/relazione.md"]
    md = z.read("relazione/relazione.md").decode()
    assert "](assets/immagine_001.png)" in md and "<!--" not in md
    meta = json.loads(z.read("relazione/metadata.json"))
    assert meta == meta | {"source": "relazione.pdf", "format": "pdf", "pages": 2, "mode": "auto"}
    assert z.testzip() is None


def test_multi_document_zip_folders_duplicates_and_failures(client):
    j = run_job(client, {
        "Documenti/relazione.pdf": "digital_it.pdf",
        "Documenti/relazione.docx": "office_report_it.docx",       # same stem, same folder
        "Documenti/sub/relazione.pdf": "digital_en_twocol.pdf",    # same name, other folder
        "Documenti/presentazione.pptx": "office_slides_en.pptx",
        "Documenti/rotto.pdf": "broken_truncated.pdf",
        "Documenti/Città è #1.txt": "note.txt",
    })
    states = {f["rel_path"]: f["state"] for f in j["files"]}
    assert states.pop("Documenti/rotto.pdf") == "error" and set(states.values()) == {"done"}
    r = client.get(f"/api/jobs/{j['id']}/zip")
    z = zip_of(r)
    assert "conversione.zip" in r.headers["content-disposition"]
    names = set(z.namelist())
    folders = {n.rsplit("/", 1)[0] for n in names if n.endswith(".md")}
    assert folders == {"Documenti/relazione (pdf)", "Documenti/relazione (docx)", "Documenti/sub/relazione",
                       "Documenti/presentazione", "Documenti/Città è _1"}
    for f in folders:
        stem = f.rsplit("/", 1)[-1]
        assert f"{f}/{stem}.md" in names and f"{f}/metadata.json" in names
    # Nothing but documents, their assets, their metadata and the README.
    assert all(n == "README.txt" or n.endswith((".md", "metadata.json")) or "/assets/" in n for n in names)
    assert not any(k in n for n in names for k in ("report", "docling", "pages/", ".tmp", "lock"))
    readme = z.read("README.txt").decode()
    assert "Documenti/rotto.pdf" in readme and "PDF illeggibile" in readme and "Documenti/relazione (docx)/" in readme


def test_single_file_download_and_preview(client):
    j = run_job(client, {"slide.pptx": "office_slides_en.pptx"})
    fid = j["files"][0]["id"]
    z = zip_of(client.get(f"/api/files/{fid}/zip"))
    assert sorted(z.namelist()) == ["slide/metadata.json", "slide/slide.md"]
    assert "## Slide 2" in client.get(f"/api/files/{fid}/markdown").text


def test_failed_job_offers_no_zip(client):
    j = run_job(client, {"rotto.pdf": "broken_truncated.pdf", "vuoto.pdf": b""})
    r = client.get(f"/api/jobs/{j['id']}/zip")
    assert r.status_code == 409 and "Nessun documento" in r.json()["detail"]
    assert client.get(f"/api/files/{j['files'][0]['id']}/zip").status_code == 409


def test_zip_refused_while_running(client):
    job = client.post("/api/jobs", json={"mode": "advanced"}).json()["id"]
    client.post(f"/api/jobs/{job}/files", files=[("files", ("lungo.pdf", (DATA / "scan_long_it.pdf").read_bytes()))])
    r = client.get(f"/api/jobs/{job}/zip")
    assert r.status_code == 409 and "in corso" in r.json()["detail"]
    client.post(f"/api/jobs/{job}/cancel")


def test_concurrent_downloads_identical(client):
    j = run_job(client, {"a.pdf": "digital_it.pdf", "b.pdf": "digital_en_twocol.pdf"})
    results = [None] * 4

    def get(i):
        results[i] = client.get(f"/api/jobs/{j['id']}/zip").content
    ts = [threading.Thread(target=get, args=(i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    contents = [{n: zipfile.ZipFile(io.BytesIO(b)).read(n) for n in zipfile.ZipFile(io.BytesIO(b)).namelist()}
                for b in results]
    assert all(c == contents[0] for c in contents) and len(contents[0]) == 6  # README + 2 x (md, metadata) + 1 image


def test_path_traversal_and_asset_confinement(client):
    j = run_job(client, {"r.pdf": "digital_it.pdf"})
    fid = j["files"][0]["id"]
    assert client.get(f"/api/files/{fid}/assets/immagine_001.png").status_code == 200
    assert client.get(f"/api/files/{fid}/assets/..%2Fmetadata.json").status_code == 404
    job = client.post("/api/jobs", json={"mode": "fast"}).json()["id"]
    r = client.post(f"/api/jobs/{job}/files", files=[("files", ("../evil.pdf", b"x"))]).json()
    assert r["rejected"][0]["name"] == "../evil.pdf"


def test_retention_cleanup_spares_active_downloads(client, monkeypatch):
    from app import jobs
    from app.main import scheduler, store
    j = run_job(client, {"x.txt": "note.txt"})
    store.x("UPDATE jobs SET created=? WHERE id=?", (time.time() - 30 * 86400, j["id"]))
    jobs.download_started(j["id"])
    try:
        assert j["id"] not in scheduler.cleanup()            # being downloaded: kept
        assert (config.RESULTS / j["id"]).exists()
    finally:
        jobs.download_finished(j["id"])
    assert j["id"] in scheduler.cleanup()                    # expired: uploads, work, results removed
    assert not any((root / j["id"]).exists() for root in (config.UPLOADS, config.WORK, config.RESULTS))
    assert client.get(f"/api/jobs/{j['id']}").status_code == 404


def test_streaming_zip_is_chunked_not_buffered():
    """The archive is produced piece by piece from files on disk (no temp file, flat memory)."""
    from app.export import stream_zip
    big = Path(config.TEMP) / "big.bin"
    big.write_bytes(b"\0" * (8 << 20))
    try:
        pieces = list(stream_zip([("a/big.bin", big), ("README.txt", b"ciao")]))
        assert len(pieces) > 4 and max(len(p) for p in pieces) < 4 << 20
        z = zipfile.ZipFile(io.BytesIO(b"".join(pieces)))
        assert z.read("a/big.bin") == b"\0" * (8 << 20) and z.read("README.txt") == b"ciao"
    finally:
        big.unlink()


def test_foreign_host_is_rejected(client):
    """DNS rebinding: a page on another domain that resolves to 127.0.0.1 must not read the API."""
    assert client.get("/api/jobs", headers={"host": "evil.example"}).status_code == 400
    assert client.get("/api/jobs", headers={"host": "localhost:8000"}).status_code == 200
