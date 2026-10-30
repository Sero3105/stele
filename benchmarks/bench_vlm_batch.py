"""VLM optimisation benchmark: quantisation x batch size on the same pages.

For each (model, mode, batch) a fresh subprocess converts the same rendered pages and
reports wall time, s/page, kernel peak footprint and CER per source document.

    .venv/bin/python benchmarks/bench_vlm_batch.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "benchmarks"))
DATA = ROOT / "benchmarks" / "data"
OUT = ROOT / "benchmarks" / "results"

# 7 pages: clean/noisy/poor Italian (with table), two-column English, photo-like image.
PAGES = [("scan_clean300_it.pdf", 1), ("scan_noisy150_it.pdf", 1), ("scan_noisy150_it.pdf", 2),
         ("scan_poor100_it.pdf", 1), ("scan_poor100_it.pdf", 2), ("scan_noisy150_en_twocol.pdf", 1),
         ("image_en_photo.jpg", 1)]

CONFIGS = [
    ("paddleocr-vl", "layout", 1), ("paddleocr-vl-4bit", "layout", 1), ("paddleocr-vl-4bit", "layout", 4),
    ("paddleocr-vl-4bit", "layout", 8),
    ("glm-ocr", "layout", 1), ("glm-ocr-8bit", "layout", 1), ("glm-ocr-4bit", "layout", 1),
    ("glm-ocr-4bit", "layout", 8),
    ("glm-ocr-4bit", "page", 1), ("glm-ocr-8bit", "page", 1),
]


def page_gt(f: str, n: int) -> str:
    import make_dataset as ds
    if "_it" in f:
        if n == 1:
            return "\n".join([ds.IT_TITLE] + [x for h, ps in ds.IT_SECTIONS for x in (h, *ps)]
                             + [" ".join(r) for r in ds.IT_TABLE])
        return "\n".join([x for h, ps in ds.IT_SECTIONS_2 for x in (h, *ps)] + ds.IT_LIST)
    return (DATA / f"{Path(f).stem}.gt.txt").read_text()


def worker(key: str, mode: str, batch: int, out: Path, max_side: int | None = None) -> None:
    from PIL import Image

    from app import config
    from app.analyzer import render_pages
    from app.engines.vlm_engine import VlmEngine
    from app.resources import EngineResources, footprint, peak_footprint
    from bench_engines import cer_wer

    tmp = OUT / "pages"
    images = []
    for f, n in PAGES:
        if f.endswith(".pdf"):
            p = render_pages(DATA / f, [n], tmp / Path(f).stem, config.OCR_DPI)[0]
        else:
            p = DATA / f
        images.append(Image.open(p).convert("RGB"))
    eng = VlmEngine(key, "glm" if key.startswith("glm") else "paddle", mode,
                    EngineResources(4000, 2000, 1), max_side=max_side)
    base = footprint()
    t = time.perf_counter()
    eng.run(lambda m: None)
    load = time.perf_counter() - t
    model_mb = (footprint() - base) // 2**20
    t = time.perf_counter()
    res = eng.pages_markdown(images)
    wall = time.perf_counter() - t
    scores = {f"{f}#{n}": cer_wer(md, page_gt(f, n))[0] for (f, n), (md, _) in zip(PAGES, res)}
    out.write_text(json.dumps(dict(model=key, mode=mode, batch=batch, load_s=round(load, 2), model_mb=model_mb,
                                   wall_s=round(wall, 2), s_per_page=round(wall / len(PAGES), 2),
                                   peak_mb=peak_footprint() // 2**20, cer=scores,
                                   markdown=[md for md, _ in res]), ensure_ascii=False, indent=1))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        worker(sys.argv[2], sys.argv[3], int(sys.argv[4]), Path(sys.argv[5]),
               int(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6] != "0" else None)
        return
    OUT.mkdir(exist_ok=True)
    configs = CONFIGS
    if len(sys.argv) > 1:  # e.g. glm-ocr-4bit:page:1:1280
        configs = [tuple(c.split(":")) for c in sys.argv[1:]]
    for key, mode, batch, *side in configs:
        side = side[0] if side else "0"
        out = OUT / f"vlmopt_{key}_{mode}_b{batch}_s{side}.json"
        p = subprocess.run([sys.executable, __file__, "worker", key, mode, str(batch), str(out), side],
                           capture_output=True, text=True)
        if p.returncode:
            print(f"{key} {mode} b{batch}: FAILED\n{p.stderr[-1500:]}", flush=True)
            continue
        r = json.loads(out.read_text())
        print(f"{key:18s} {mode:6s} b{batch} side={side}: {r['s_per_page']:5.2f} s/p  wall={r['wall_s']:6.1f}s "
              f"load={r['load_s']}s model={r['model_mb']}MB peak={r['peak_mb']}MB cer={r['cer']}", flush=True)


if __name__ == "__main__":
    main()
