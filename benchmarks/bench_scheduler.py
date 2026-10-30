"""Scheduler / parallelism benchmark through the real job queue (no HTTP).

Each scenario runs in a fresh process with its own storage; the kernel-tracked lifetime
peak footprint of that process (models + workers + renders; the sampled RM peak also
includes Vision helper child processes) is compared with the budget.

    .venv/bin/python benchmarks/bench_scheduler.py                # all scenarios, 20 GB budget
    STELE_MEMORY_BUDGET_GB=6 .venv/bin/python benchmarks/bench_scheduler.py heavy2_light5
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "benchmarks" / "data"
OUT = ROOT / "benchmarks" / "results"

LIGHT = ["digital_it.pdf", "digital_en_twocol.pdf", "office_report_it.docx", "office_slides_en.pptx",
         "office_data.xlsx", "digital_formula_en.pdf"]
HEAVY = ["scan_poor100_it.pdf", "scan_clean300_it.pdf", "scan_noisy150_it.pdf"]
MIXED = LIGHT + HEAVY + ["scan_noisy150_en_twocol.pdf", "image_en_photo.jpg", "image_it_table.png",
                         "mixed_digital_scan.pdf", "broken_truncated.pdf"]


def pick(pool: list[str], n: int) -> list[str]:
    return [pool[i % len(pool)] for i in range(n)]


STRESS = DATA / "stress"


def big_pdf(pages: int) -> str:
    """A long PDF made of real dataset pages, cycling 4 digital : 5 scanned prose : 1 scanned table."""
    import pypdfium2 as pdfium
    STRESS.mkdir(exist_ok=True)
    name = f"stress/big_{pages}.pdf"
    if (DATA / name).exists():
        return name
    pattern = [("digital_it.pdf", 0), ("digital_it.pdf", 1), ("digital_en_twocol.pdf", 0),
               ("digital_formula_en.pdf", 0)] + [("scan_long_it.pdf", i) for i in range(5)] + [
               ("scan_noisy150_it.pdf", 0)]
    out = pdfium.PdfDocument.new()
    srcs = {f: pdfium.PdfDocument(DATA / f) for f, _ in pattern}
    for i in range(pages):
        f, idx = pattern[i % len(pattern)]
        out.import_pages(srcs[f], [idx])
    out.save(DATA / name)
    return name


SCENARIOS = {
    "light2": (pick(LIGHT, 2), "auto"),
    "light5": (pick(LIGHT, 5), "auto"),
    "light20": (pick(LIGHT, 20), "auto"),
    "heavy2": (pick(HEAVY, 2), "advanced"),
    "heavy2_light5": (pick(HEAVY, 2) + pick(LIGHT, 5), "auto"),
    "mixed50": (pick(MIXED, 50), "auto"),
    "big100": (["@100"], "auto"),
    "big300": (["@300"], "auto"),
    "big500": (["@500"], "auto"),
    "big_all": (["@100", "@300", "@500"], "auto"),  # the three long PDFs at the same time
}


def worker(name: str, workers: int, out: Path) -> None:
    os.environ["STELE_STORAGE"] = tempfile.mkdtemp(prefix=f"bench_{name}_")
    sys.path.insert(0, str(ROOT))
    from pathlib import PurePosixPath

    from app import config
    from app.jobs import JobStore, Scheduler
    from app.resources import manager, peak_footprint
    files, mode = SCENARIOS[name]
    files = [big_pdf(int(f[1:])) if f.startswith("@") else f for f in files]
    from app.pipeline import VISION
    store = JobStore()
    sched = Scheduler(store, workers=workers)
    rm = manager()
    job = store.create_job(mode)
    for i, f in enumerate(files):
        fid, dest = store.add_file(job, PurePosixPath(f"{i:03d}_{Path(f).name}"), 0)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((DATA / f).read_bytes())
        store.mark_uploaded(fid, dest.stat().st_size)
    t = time.perf_counter()
    sched.start()
    max_conc, samples = 0, []
    while True:
        j = store.job(job)
        snap = rm.snapshot()
        max_conc = max(max_conc, len(snap["active_jobs"]))
        samples.append(snap["footprint"])
        if not (j["counts"].get("queued") or j["counts"].get("running")):
            break
        time.sleep(0.25)
    wall = time.perf_counter() - t
    sched.stop()
    engines: dict[str, int] = {}
    for f in j["files"]:
        for e in (f["engines"] or "").split(", "):
            if e:
                engines[e] = engines.get(e, 0) + 1
    out.write_text(json.dumps({
        "scenario": name, "mode": mode, "files": len(files), "workers": workers,
        "budget_gb": config.MEMORY_BUDGET / config.GB, "wall_s": round(wall, 1),
        "files_per_min": round(60 * len(files) / wall, 1), "counts": j["counts"],
        "peak_gb": round(peak_footprint() / config.GB, 2), "rm_peak_gb": round(rm.peak_seen / config.GB, 2),
        "sampled_max_gb": round(max(samples) / config.GB, 2), "max_concurrent_leases": max_conc,
        "vision_child_peak_mb": VISION.child_peak // config.MB,
        # Upper bound: main process lifetime peak + the 3 Vision helpers allowed at once at their peak.
        "bound_gb": round((peak_footprint() + 3 * VISION.child_peak) / config.GB, 2),
        "pages": sum((f["summary"] or {}).get("pages", 0) for f in j["files"]),
        "memory_waits": rm.waits, "engines": engines,
        "errors": [f"{f['rel_path']}: {f['error']}" for f in j["files"] if f["state"] == "error"],
    }, indent=1))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        worker(sys.argv[2], int(sys.argv[3]), Path(sys.argv[4]))
        return
    names = [a for a in sys.argv[1:] if not a.startswith("-")] or list(SCENARIOS)
    workers = int(os.environ.get("STELE_WORKERS", "4"))
    budget = os.environ.get("STELE_MEMORY_BUDGET_GB", "20")
    for name in names:
        out = OUT / f"sched_{name}_w{workers}_b{budget}.json"
        p = subprocess.run([sys.executable, __file__, "worker", name, str(workers), str(out)],
                           capture_output=True, text=True)
        if p.returncode:
            print(f"{name}: FAILED\n{p.stderr[-2000:]}", flush=True)
            continue
        r = json.loads(out.read_text())
        ok = "OK" if r["bound_gb"] <= r["budget_gb"] else "OVER BUDGET"
        print(f"{name:14s} w={workers} budget={r['budget_gb']:.0f}GB  {r['wall_s']:6.1f}s  {r['files_per_min']:5.1f} file/min  "
              f"pages={r['pages']} peak={r['peak_gb']}GB sampled={r['sampled_max_gb']}GB +helpers<={r['bound_gb']}GB [{ok}]  "
              f"conc={r['max_concurrent_leases']} waits={r['memory_waits']}  "
              f"{r['counts']}  {r['engines']}", flush=True)


if __name__ == "__main__":
    main()
