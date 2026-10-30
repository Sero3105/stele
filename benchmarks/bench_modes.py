"""End-to-end routing benchmark: the real pipeline in each mode on the same documents.

Each mode runs in its own process (cold start included once, reported separately as the
first file's extra time is visible in per-file seconds).

    .venv/bin/python benchmarks/bench_modes.py
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
sys.path.insert(0, str(ROOT / "benchmarks"))
DATA = ROOT / "benchmarks" / "data"
OUT = ROOT / "benchmarks" / "results"
FILES = sorted(p.name for p in DATA.glob("scan_*.pdf")) + [
    "image_it_table.png", "image_en_photo.jpg", "mixed_digital_scan.pdf",
    "digital_it.pdf", "digital_en_twocol.pdf", "digital_formula_en.pdf"]
# scan_long_it.pdf (12 prose pages) is part of the scan_*.pdf glob above.


def worker(mode: str, out: Path) -> None:
    os.environ["STELE_STORAGE"] = tempfile.mkdtemp(prefix=f"modes_{mode}_")
    sys.path.insert(0, str(ROOT))
    from app.pipeline import Context, convert_file
    from app.resources import peak_footprint
    from bench_engines import cer_wer, expectations, heading_score, table_score
    rows = []
    t_all = time.perf_counter()
    for f in FILES:
        root = Path(os.environ["STELE_STORAGE"])
        ctx = Context(out_dir=root / "out" / f, work_dir=root / "work" / f, name=f, mode=mode)
        t = time.perf_counter()
        s = convert_file(DATA / f, ctx)
        dt = time.perf_counter() - t
        md = Path(s["markdown"]).read_text()
        data = s["report"]
        gt = (DATA / f"{Path(f).stem}.gt.txt").read_text()
        table, heads = expectations(f)
        cer, wer = cer_wer(md, gt)
        rows.append(dict(file=f, seconds=round(dt, 2), pages=s["pages"], cer=cer, wer=wer,
                         table=table_score(md, table) if table else None, headings=heading_score(md, heads),
                         engines=[p["engine"] for p in data["pages"]],
                         escalations=[(p["page"], p["escalated_from"]["engine"], p["engine"], p["escalated_from"]["reason"])
                                      for p in data["pages"] if "escalated_from" in p]))
    out.write_text(json.dumps(dict(mode=mode, total_s=round(time.perf_counter() - t_all, 1),
                                   peak_mb=peak_footprint() // 2**20, files=rows), ensure_ascii=False, indent=1))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        worker(sys.argv[2], Path(sys.argv[3]))
        return
    for mode in sys.argv[1:] or ["fast", "auto", "advanced"]:
        out = OUT / f"modes_{mode}.json"
        p = subprocess.run([sys.executable, __file__, "worker", mode, str(out)], capture_output=True, text=True)
        if p.returncode:
            print(f"{mode}: FAILED\n{p.stderr[-2500:]}")
            continue
        r = json.loads(out.read_text())
        pages = sum(f["pages"] for f in r["files"])
        cers = [f["cer"] for f in r["files"]]
        print(f"== {mode}: total {r['total_s']}s for {pages} pages ({r['total_s'] / pages:.2f} s/page incl. model loads), "
              f"peak {r['peak_mb']} MB, mean CER {sum(cers) / len(cers):.4f}")
        for f in r["files"]:
            print(f"   {f['file']:30s} {f['seconds']:6.2f}s cer={f['cer']:.4f} table={f['table']} head={f['headings']} "
                  f"{'/'.join(sorted(set(f['engines'])))}  esc={f['escalations']}")


if __name__ == "__main__":
    main()
