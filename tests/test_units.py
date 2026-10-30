"""Fast unit tests: routing policy, quality signals, resource admission, parsers."""

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
    assert unloaded == ["paddle"] and not rm.is_loaded("paddle")


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
