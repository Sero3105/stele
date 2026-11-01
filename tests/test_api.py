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
    return wait_job(client, job)


def wait_job(client, job: str) -> dict:
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
    assert sorted(z.namelist()) == ["relazione/assets/immagine_001.png", "relazione/assets/tabella_001.csv",
                                    "relazione/metadata.json", "relazione/relazione.md"]
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
    assert sorted(z.namelist()) == ["slide/assets/tabella_001.csv", "slide/metadata.json", "slide/slide.md"]
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
    assert all(c == contents[0] for c in contents) and len(contents[0]) == 8  # README + 2 x (md, metadata, table CSV) + 1 image


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


def test_reuse_retry_page_images_search_and_keep(client):
    j = run_job(client, {"a.pdf": "digital_it.pdf", "rotto.pdf": "broken_truncated.pdf"}, mode="fast")
    files = {f["rel_path"]: f for f in j["files"]}
    a, broken = files["a.pdf"], files["rotto.pdf"]
    # Original pages for the preview.
    r = client.get(f"/api/files/{a['id']}/pages/2")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    assert client.get(f"/api/files/{a['id']}/pages/3").status_code == 404
    # The same file again, under another name: the result is copied, renamed after the new file.
    j2 = run_job(client, {"copia.pdf": "digital_it.pdf"}, mode="fast")
    copy = j2["files"][0]
    assert copy["summary"]["reused"] and copy["state"] == "done"
    z = zip_of(client.get(f"/api/files/{copy['id']}/zip"))
    assert "copia/copia.md" in z.namelist() and json.loads(z.read("copia/metadata.json"))["source"] == "copia.pdf"
    # Retry a failed file; reconvert a done one in another mode.
    assert client.post(f"/api/files/{broken['id']}/retry").status_code == 200
    assert client.post(f"/api/files/{a['id']}/retry", json={"mode": "advanced"}).status_code == 200
    j = wait_job(client, j["id"])
    files = {f["rel_path"]: f for f in j["files"]}
    assert files["rotto.pdf"]["state"] == "error" and "PDF illeggibile" in files["rotto.pdf"]["error"]
    assert files["a.pdf"]["state"] == "done" and files["a.pdf"]["mode"] == "advanced" and j["mode"] == "fast"
    assert client.post(f"/api/files/{a['id']}/retry", json={"mode": "nessuna"}).status_code == 409
    # Full-text search over every converted document.
    hits = client.get("/api/search", params={"q": "intensita energet"}).json()
    assert {a["id"], copy["id"]} <= {h["id"] for h in hits}
    # Kept jobs survive the retention cleanup.
    from app.main import scheduler, store
    assert client.post(f"/api/jobs/{j['id']}/keep", json={"keep": True}).status_code == 200
    store.x("UPDATE jobs SET created=? WHERE id=?", (time.time() - 30 * 86400, j["id"]))
    assert j["id"] not in scheduler.cleanup() and client.get(f"/api/jobs/{j['id']}").json()["keep"] == 1


def test_rating_compare_restore_and_edit(client):
    j = run_job(client, {"scansione.pdf": "scan_clean300_it.pdf"}, mode="fast")
    f = j["files"][0]
    fid = f["id"]
    assert client.get(f"/api/files/{fid}/compare").status_code == 404  # nothing to compare yet
    assert client.post(f"/api/files/{fid}/feedback", json={"rating": "boh"}).status_code == 400
    # So-so: the doubtful pages (all, if none is listed) are read again by a stronger engine.
    assert client.post(f"/api/files/{fid}/feedback", json={"rating": "soso"}).json() == {"action": "recheck"}
    f = wait_job(client, j["id"])["files"][0]
    s = f["summary"]
    assert f["state"] == "done" and s["previous"] and s["rechecked_pages"] >= 1 and f["feedback"] is None
    c = client.get(f"/api/files/{fid}/compare").json()
    assert c["previous"]["mode"] == c["current"]["mode"] == "fast" and isinstance(c["diff"], list)
    before = client.get(f"/api/files/{fid}/markdown").text
    # Back to the previous version, then forward again: restore swaps the two.
    assert client.post(f"/api/files/{fid}/restore").status_code == 200
    restored = client.get(f"/api/files/{fid}/markdown").text
    assert client.get(f"/api/jobs/{j['id']}").json()["files"][0]["summary"]["rechecked_pages"] is None
    assert client.post(f"/api/files/{fid}/restore").status_code == 200
    assert client.get(f"/api/files/{fid}/markdown").text == before != restored
    # Improve in Fast mode: reconverted in Advanced.
    assert client.post(f"/api/files/{fid}/feedback", json={"rating": "improve"}).json() == {"action": "reconvert"}
    f = wait_job(client, j["id"])["files"][0]
    assert f["state"] == "done" and f["mode"] == "advanced"
    # Good: kept as it is.
    assert client.post(f"/api/files/{fid}/feedback", json={"rating": "good"}).json() == {"action": "none"}
    assert client.get(f"/api/jobs/{j['id']}").json()["files"][0]["feedback"] == "good"
    # Hand correction: saved, searchable, CSVs follow the text.
    text = client.get(f"/api/files/{fid}/markdown").text + "\nParolaCorrettaAMano\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
    assert client.put(f"/api/files/{fid}/markdown", content=text.encode()).status_code == 200
    assert client.get(f"/api/files/{fid}/markdown").text == text
    assert fid in {h["id"] for h in client.get("/api/search", params={"q": "parolacorrettaamano"}).json()}
    z = zip_of(client.get(f"/api/files/{fid}/zip"))
    assert any(n.endswith(".csv") and b"1,2" in z.read(n) for n in z.namelist())
    assert "edited_at" in json.loads(z.read("scansione/metadata.json"))
    assert client.put(f"/api/files/{fid}/markdown", content=b"  ").status_code == 400


def test_office_files_cannot_be_reread_and_chapters_cannot_be_edited(client):
    j = run_job(client, {"nota.txt": "note.txt"})
    fid = j["files"][0]["id"]
    assert client.post(f"/api/files/{fid}/feedback", json={"rating": "soso"}).status_code == 409
    job = client.post("/api/jobs", json={"mode": "fast", "split": True}).json()["id"]
    client.post(f"/api/jobs/{job}/files", files=[("files", ("r.pdf", (DATA / "digital_it.pdf").read_bytes()))])
    f = wait_job(client, job)["files"][0]
    assert f["summary"]["chapters"] == 4
    assert client.put(f"/api/files/{f['id']}/markdown", content=b"x").status_code == 409
    full = client.get(f"/api/files/{f['id']}/markdown", params={"full": True}).text
    assert "## Capitoli" in full and "Questa relazione" in full
    names = zip_of(client.get(f"/api/files/{f['id']}/zip")).namelist()
    assert "r/capitoli/r - 01 Introduzione.md" in names
    assert client.post("/api/jobs", json={"mode": "auto", "pages": "9-1"}).status_code == 400
