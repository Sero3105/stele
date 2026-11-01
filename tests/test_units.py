"""Fast unit tests: routing policy, quality signals, resource admission, parsers."""

import json
import threading
import time
from pathlib import Path, PurePosixPath

import pytest

from app import config
from app.analyzer import analyze, classify_page
from app.engines.vision_engine import blocks_to_markdown, md_table
from app.engines.vlm_engine import clean_output
from app.jobs import JobStore, safe_relative_path
from app.quality import misspelled_ratio, repetition_ratio, text_quality
from app.resources import ResourceManager, footprint

DATA = Path(__file__).resolve().parent.parent / "benchmarks" / "data"
MB = config.MB


# Routing policy: see tests/test_router.py


# ------------------------------------------------------------------ quality
def test_spellcheck_flags_ocr_errors():
    good = "Questa relazione descrive il consumo energetico degli edifici comunali nel corso dell'anno."
    bad = "Questa refazione descrive il consumo energetico degli edifici comunali, temici e cimatizzazione."
    assert misspelled_ratio(good, "it")[0] == 0.0
    assert misspelled_ratio(bad, "it")[0] > 0.1


def test_spellcheck_counts_utf16_units():
    # Emoji and math letters like 𝑥 are 2 UTF-16 units: the last words must still be checked.
    text = "Questa relazione descrive il consumo energetico degli edifici comunali nel cimatizzazione."
    assert misspelled_ratio("😀" * 20 + " " + text, "it") == misspelled_ratio(text, "it")
    assert misspelled_ratio(text, "it")[0] > 0


def test_spellcheck_thread_safe():
    out = []
    ts = [threading.Thread(target=lambda: out.append(misspelled_ratio("The northern beach lost width " * 10, "en")))
          for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert set(out) == {(0.0, 50)}


def test_text_quality_detects_junk_and_loops():
    assert text_quality("Normale testo di prova con parole vere e abbastanza lunghe.") > 0.9
    assert text_quality("(cid:12)(cid:44)(cid:91) ��� (cid:3)(cid:9)") < 0.3
    loop = "la tabella seguente riporta " * 40
    assert repetition_ratio(loop) > 0.5 and text_quality(loop) < 0.3


# ------------------------------------------------------------------ parsers
def test_clean_output_tables_and_math():
    assert clean_output("<table><tr><td>a</td><td>b</td></tr><tr><td>1</td><td>2</td></tr></table>") == \
        "| a | b |\n|---|---|\n| 1 | 2 |"
    assert clean_output("<fcel>A<fcel>B<nl><fcel>1<ecel><nl>") == "| A | B |\n|---|---|\n| 1 |  |"
    assert "Superficie (m²)" in clean_output(r"| \(Superficie (m^{{2}})\) |")
    assert r"\frac" in clean_output(r"$E = \frac{1}{2} m v^2$")


def test_vision_blocks_no_duplicate_table_cells():
    box = lambda x, y, w, h: {"x": x, "y": y, "w": w, "h": h}
    res = {"lines": [], "blocks": [
        {"kind": "table", "box": box(0.1, 0.5, 0.8, 0.2), "rows": [["A", "B"], ["1", "2"]]},
        {"kind": "paragraph", "box": box(0.1, 0.1, 0.8, 0.05), "text": "Intro"},
        {"kind": "paragraph", "box": box(0.15, 0.55, 0.1, 0.02), "text": "A"},
        {"kind": "paragraph", "box": box(0.5, 0.55, 0.1, 0.02), "text": "B"},
        {"kind": "paragraph", "box": box(0.1, 0.8, 0.8, 0.05), "text": "Fine"}]}
    md = blocks_to_markdown(res)
    assert md == "Intro\n\n" + md_table([["A", "B"], ["1", "2"]]) + "\n\nFine"


def test_vision_helper_jsonl_with_unicode_line_separators(monkeypatch):
    # Swift JSONEncoder emits U+2028/U+2029 raw; str.splitlines() would cut the
    # JSON string there and raise "Unterminated string starting at: ...".
    import json as _json
    import subprocess as _sp

    from app.engines import vision_engine

    row = {"image": "p.png", "lines": [{"text": "prima seconda", "confidence": 0.9,
                                        "box": {"x": 0, "y": 0, "w": 1, "h": 1}}],
           "blocks": [], "error": None}
    stdout = _json.dumps(row, ensure_ascii=False) + "\n{\"peak_footprint\": 1}\n"
    monkeypatch.setattr(vision_engine.subprocess, "run",
                        lambda *a, **k: _sp.CompletedProcess(a, 0, stdout=stdout, stderr=""))
    monkeypatch.setattr(vision_engine.VisionEngine, "run", lambda self, fn: fn(None))
    rows = vision_engine.VisionEngine().recognize([Path("p.png")])
    assert rows[0]["lines"][0]["text"] == "prima seconda"


# ------------------------------------------------------------------ analyzer
@pytest.mark.parametrize("name,family,kinds,error", [
    ("digital_it.pdf", "pdf", {"digital": 2}, False),
    ("scan_noisy150_it.pdf", "pdf", {"scanned": 2}, False),
    ("mixed_digital_scan.pdf", "pdf", {"digital": 3, "scanned": 1}, False),
    ("image_it_table.png", "image", {"scanned": 1}, False),
    ("office_report_it.docx", "office", {}, False),
    ("broken_truncated.pdf", "pdf", {}, True),
    ("empty.pdf", "unsupported", {}, True),
    ("unsupported.xyz", "unsupported", {}, True),
])
def test_analyze(name, family, kinds, error):
    info = analyze(DATA / name)
    assert info.family == family and info.counts() == kinds and bool(info.error) == error


def test_mixed_pdf_page_order():
    info = analyze(DATA / "mixed_digital_scan.pdf")
    assert [p.kind for p in info.pages] == ["digital", "digital", "scanned", "digital"]


def test_classify_broken_text_layer_as_scanned():
    assert classify_page(500, 0.0, 0.2) == "scanned"
    assert classify_page(0, 0.0, 0.0) == "blank"


# ------------------------------------------------------------------ resources
def test_lease_rejects_job_over_budget():
    rm = ResourceManager(budget=footprint() + 100 * MB, emergency=footprint() + 80 * MB)
    with pytest.raises(MemoryError):
        with rm.lease("big", 0, 500 * MB, 1):
            pass


def test_lease_waits_until_memory_is_released():
    base = footprint()
    rm = ResourceManager(budget=base + 3000 * MB, emergency=base + 2000 * MB)
    order = []

    def job(name, hold):
        with rm.lease(name, 0, 1200 * MB, 4):
            order.append(f"start {name}")
            time.sleep(hold)
            order.append(f"end {name}")

    t1 = threading.Thread(target=job, args=("a", 0.6))
    t1.start()
    time.sleep(0.1)
    t2 = threading.Thread(target=job, args=("b", 0.0))  # 1.2+1.2 GB > 2 GB emergency: must queue
    t2.start()
    t1.join(); t2.join()
    assert order == ["start a", "end a", "start b", "end b"]
    assert rm.waits >= 1


def test_max_parallel_per_engine():
    rm = ResourceManager()
    active, peak = [0], [0]
    lock = threading.Lock()

    def job():
        with rm.lease("vlm", 0, 10 * MB, 1):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.1)
            with lock:
                active[0] -= 1

    ts = [threading.Thread(target=job) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 1


def test_idle_model_is_evicted_for_room():
    base = footprint()
    rm = ResourceManager(budget=base + 3000 * MB, emergency=base + 2000 * MB)
    unloaded = []
    with rm.lease("paddle", 1500 * MB, 100 * MB, 1):
        rm.model_now_resident("paddle", 1500 * MB, lambda: unloaded.append("paddle"))
    with rm.lease("glm", 1500 * MB, 100 * MB, 1):  # doesn't fit next to idle paddle -> evict it
        pass
    assert unloaded == ["paddle"] and "paddle" not in [m["name"] for m in rm.snapshot()["models"]]


def test_eviction_unload_does_not_block_model_registration():
    # Regression: unloading an evicted model must not run while holding the
    # manager lock, or a model loading in another thread (which registers
    # itself under that lock) deadlocks against the unload.
    base = footprint()
    rm = ResourceManager(budget=base + 3000 * MB, emergency=base + 2000 * MB)
    unload_started, registered, saw = threading.Event(), threading.Event(), []

    def unload():
        unload_started.set()
        saw.append(registered.wait(timeout=5))

    with rm.lease("paddle", 1500 * MB, 100 * MB, 1):
        rm.model_now_resident("paddle", 1500 * MB, unload)
    t = threading.Thread(target=lambda: rm.lease("glm", 1500 * MB, 100 * MB, 1).__enter__())
    t.start()
    assert unload_started.wait(timeout=5)
    rm.model_now_resident("loader", 10 * MB, lambda: None)  # blocks forever on the buggy code
    registered.set()
    t.join(timeout=5)
    assert not t.is_alive() and saw == [True]


# ------------------------------------------------------------------ jobs
def test_safe_relative_path():
    assert safe_relative_path("Documenti/a.pdf") == PurePosixPath("Documenti/a.pdf")
    for bad in ("../x.pdf", "/etc/passwd", "a//b.pdf", "", "a/./b.pdf", ".DS_Store"):
        with pytest.raises(ValueError):
            safe_relative_path(bad)


def test_duplicate_names_and_restart_recovery(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    job = store.create_job("auto")
    f1, p1 = store.add_file(job, PurePosixPath("dir/a.pdf"), 1)
    f2, p2 = store.add_file(job, PurePosixPath("dir/a.pdf"), 1)
    assert p1.name == "a.pdf" and p2.name == "a (2).pdf"
    store.mark_uploaded(f1, 1)
    store.mark_uploaded(f2, 1)
    assert store.claim()["id"] == f1  # now "running"
    store2 = JobStore(tmp_path / "db.sqlite3")  # simulated restart
    assert store2.recover() == 1
    assert store2.file(f1)["state"] == "queued"


def test_engines_never_load_models_concurrently():
    from app.engines.base import Engine
    active, peak, lock = [0], [0], threading.Lock()

    class Slow(Engine):
        def _load(self):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.2)
            with lock:
                active[0] -= 1
            return object()

    engines = [Slow() for _ in range(3)]
    for i, e in enumerate(engines):
        e.name = f"slow{i}"
    ts = [threading.Thread(target=e.run, args=(lambda m: m,)) for e in engines]
    [t.start() for t in ts]
    [t.join() for t in ts]
    [e.unload() for e in engines]
    assert peak[0] == 1


def test_claim_round_robin_across_jobs(tmp_path):
    """A single file uploaded after a big folder must not wait for the whole folder."""
    store = JobStore(tmp_path / "db.sqlite3")
    big, small = store.create_job("auto"), store.create_job("auto")
    folder = [store.add_file(big, PurePosixPath(f"dir/{i}.pdf"), 1)[0] for i in range(25)]
    single = store.add_file(small, PurePosixPath("solo.pdf"), 1)[0]
    for fid in folder + [single]:
        store.mark_uploaded(fid, 1)
    assert [store.claim()["id"] for _ in range(4)] == [folder[0], single, folder[1], folder[2]]


def test_spellcheck_first_use_from_many_threads_in_fresh_process():
    """Regression: the very first spell check racing on 8 threads used to fail with 'NSMakeRange'."""
    import subprocess
    import sys
    code = ("import threading; from app.quality import misspelled_ratio; errs=[]\n"
            "def f():\n"
            "    try: misspelled_ratio('Questa relazione descrive il consumo energetico', 'it')\n"
            "    except Exception as e: errs.append(repr(e))\n"
            "ts=[threading.Thread(target=f) for _ in range(8)]; [t.start() for t in ts]; [t.join() for t in ts]\n"
            "assert not errs, errs")
    for _ in range(5):
        subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).resolve().parent.parent)


# ------------------------------------------------------------------ models / prune
def test_prune_removes_only_unused_repos_revisions_and_blobs(tmp_path, monkeypatch):
    import os

    from app import models
    hub = tmp_path / "hub"
    shared = hub / "blobs"

    def blob(h: str, data: bytes) -> Path:
        d = shared / h[:2]
        d.mkdir(parents=True, exist_ok=True)
        (d / h).write_bytes(data)
        (d / f"{h}.lock").write_bytes(b"")
        (d / f"{h}.refs").write_bytes(b"x")
        return d / h

    def snapshot(repo: str, rev: str, target: Path) -> Path:
        snap = hub / f"models--{repo.replace('/', '--')}" / "snapshots" / rev
        snap.mkdir(parents=True)
        (snap / "model.safetensors").symlink_to(os.path.relpath(target, snap))
        return snap

    kept = models.REGISTRY["glm-ocr-4bit"]
    keep_blob = blob("aa" + "1" * 62, b"keep")
    keep_snap = snapshot(kept.repo, kept.revision, keep_blob)
    old_blob = blob("bb" + "2" * 62, b"old")
    old_snap = snapshot(kept.repo, "0" * 40, old_blob)                        # stale revision
    cand_blob = blob("cc" + "3" * 62, b"cand")
    snapshot(models.REGISTRY["glm-ocr"].repo, "f" * 40, cand_blob)           # benchmark candidate
    stranger_blob = blob("dd" + "4" * 62, b"other")
    snapshot("someone/else", "e" * 40, stranger_blob)                        # not in the registry
    orphan = blob("ee" + "5" * 62, b"orphan")                                # no snapshot at all

    monkeypatch.setattr(models, "_hub_dir", lambda: hub)
    models.prune(dry_run=True)
    assert old_blob.exists() and cand_blob.exists() and orphan.exists()     # dry run touches nothing
    models.prune(dry_run=False)
    assert (keep_snap / "model.safetensors").read_bytes() == b"keep"
    assert all(Path(f"{keep_blob}{s}").exists() for s in ("", ".lock", ".refs"))
    assert not old_snap.exists()
    for gone in (old_blob, cand_blob, stranger_blob, orphan):
        assert not any(Path(f"{gone}{s}").exists() for s in ("", ".lock", ".refs"))
    assert not (hub / "models--mlx-community--GLM-OCR-bf16").exists()
    assert not (hub / "models--someone--else").exists()


def test_spellcheck_ignores_latex():
    md = r"The energy is $E = \frac{1}{2} m v^2$ and $$x = \sqrt{b^2 - 4ac}$$ for the roots of the equation."
    assert misspelled_ratio(md, "en")[0] == 0.0


def test_xlsx_details_keeps_chart_sheets_in_order(tmp_path):
    """Docling makes one page per sheet name, chart sheets included: the order must match."""
    import openpyxl
    from openpyxl.chart import BarChart, Reference

    from app.office import xlsx_details
    wb = openpyxl.Workbook()
    wb.active.title = "Dati"
    wb.active["A1"], wb.active["A2"] = 1, "=A1*2"
    chart = BarChart()
    chart.add_data(Reference(wb.active, min_col=1, min_row=1, max_row=2))
    wb.create_chartsheet("Grafico").add_chart(chart)  # openpyxl can't reload an empty chart sheet
    wb.create_sheet("Riepilogo")
    wb.save(tmp_path / "x.xlsx")
    sheets = xlsx_details(tmp_path / "x.xlsx")
    assert [s["name"] for s in sheets] == ["Dati", "Grafico", "Riepilogo"]
    assert sheets[0]["formulas"][0]["formula"] == "=A1*2" and sheets[2]["formulas"] == []


def test_guess_language_any_language_and_too_little_text():
    from app.quality import guess_language
    assert guess_language("Questa relazione descrive il consumo energetico degli edifici comunali nel corso "
                          "dell'anno, con dati raccolti ogni mese dai contatori.") == "it"
    assert guess_language("Ce rapport décrit la consommation d'énergie des bâtiments municipaux au cours de "
                          "l'année, avec des données recueillies chaque mois.") == "fr"
    assert guess_language("Dieser Bericht beschreibt den Energieverbrauch der städtischen Gebäude im Laufe des "
                          "Jahres, mit monatlich erfassten Daten der Zähler.") == "de"
    assert guess_language("Tabella 1 2450 182300 74,4") is None


def test_clean_output_unwraps_display_formula():
    assert clean_output(r"\[\int_0^1 x\,dx\]") == r"\int_0^1 x\,dx"
    assert clean_output(r"testo \[x\] testo") == r"testo \[x\] testo"


def test_table_csv_unescapes_pipes_and_quotes():
    from app.pipeline import table_csv
    assert table_csv([["A", "B"], ["1 \\| 2", 'dice "ciao", poi']]) == 'A,B\n1 | 2,"dice ""ciao"", poi"\n'


def test_search_index_words_prefixes_accents_and_syntax(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    job = store.create_job("auto")
    fid, _ = store.add_file(job, PurePosixPath("a.pdf"), 1)
    store.finish(fid, {"engines": []}, None, "# Relazione\n\nConsumo energetico della città, **2024**.")
    assert [h["id"] for h in store.search("citta energ")] == [fid]      # accents ignored, prefixes match
    assert "\x02" in store.search("consumo")[0]["snippet"]
    assert store.search('"AND OR ( * -') == [] and store.search("") == []  # user text is never FTS5 syntax
    assert store.search("bilancio") == []
    store.delete(job)
    assert store.search("consumo") == []


def test_kept_jobs_never_expire_and_old_databases_are_migrated(tmp_path):
    import sqlite3
    db = tmp_path / "db.sqlite3"
    old = sqlite3.connect(db)  # schema of the first release
    old.executescript("CREATE TABLE jobs (id TEXT PRIMARY KEY, created REAL, mode TEXT, cancelled INTEGER DEFAULT 0);"
                      "CREATE TABLE files (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, rel_path TEXT, size INTEGER,"
                      " state TEXT, stage TEXT DEFAULT '', pages_done INTEGER DEFAULT 0, pages_total INTEGER DEFAULT 0,"
                      " engines TEXT DEFAULT '', summary TEXT, error TEXT, created REAL, started REAL, finished REAL);"
                      "INSERT INTO jobs VALUES ('vecchio', 0, 'auto', 0);")
    old.commit()
    old.close()
    store = JobStore(db)
    new = store.create_job("fast", rag=True)
    store.x("UPDATE jobs SET created=0 WHERE id=?", (new,))
    store.keep(new, True)
    assert store.expired(60) == ["vecchio"]
    assert store.job(new)["rag"] == 1


def test_retry_requeues_in_another_mode(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    job = store.create_job("fast")
    fid, _ = store.add_file(job, PurePosixPath("a.pdf"), 1)
    store.mark_uploaded(fid, 1)
    with pytest.raises(ValueError):
        store.retry(fid)  # still queued
    store.cancel(job)
    store.retry(fid, "advanced")
    row = store.claim()
    assert row["id"] == fid and row["run_mode"] == "advanced"  # the job itself stays "fast"
    assert store.job(job)["mode"] == "fast"


def test_parse_pages():
    from app.pipeline import parse_pages
    assert parse_pages("1-3, 8", 5) == {1, 2, 3} and parse_pages(" 2 ;4-4") == {2, 4}
    for bad in ("0", "3-1", "a", "1-"):
        with pytest.raises(ValueError):
            parse_pages(bad)


def test_job_options_rating_and_recheck_reach_the_worker(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    with pytest.raises(ValueError):
        store.create_job("auto", pages="5-2")
    job = store.create_job("auto", split=True, pages="1-2")
    fid, _ = store.add_file(job, PurePosixPath("a.pdf"), 1)
    store.mark_uploaded(fid, 1)
    store.claim()
    store.finish(fid, {"engines": [], "review": []}, None, "testo")
    store.rate(fid, "soso")
    with pytest.raises(ValueError):
        store.rate(fid, "boh")
    store.retry(fid, recheck={"pages": [2], "engine": "next"})
    row = store.claim()
    assert row["split"] == 1 and row["pages"] == "1-2" and json.loads(row["recheck"]) == {"pages": [2], "engine": "next"}
    assert store.file(fid)["summary"] == {"engines": [], "review": []}  # kept for the previous version
    store.finish(fid, {"engines": []}, None, "testo")
    f = store.file(fid)
    assert f["recheck"] is None and f["feedback"] is None  # a new result is rated again
