"""Engine benchmark: accuracy (CER/WER, tables, headings), speed, load time, peak memory.

Each engine runs in its own subprocess so its kernel-tracked peak footprint is clean.

    .venv/bin/python benchmarks/bench_engines.py                 # all engines
    .venv/bin/python benchmarks/bench_engines.py apple-vision glm-ocr:layout
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "benchmarks"))

DATA = ROOT / "benchmarks" / "data"
OUT = ROOT / "benchmarks" / "results"
SCANS = sorted(p.name for p in DATA.glob("scan_*.pdf")) + ["image_it_table.png", "image_en_photo.jpg"]
DIGITAL = ["digital_it.pdf", "digital_en_twocol.pdf", "digital_formula_en.pdf"]

ENGINES = [
    "docling", "docling-ocrmac", "docling-rapidocr", "ocrmac-vision", "ocrmac-livetext",
    "apple-vision",
    "paddleocr-vl:layout", "paddleocr-vl-4bit:layout", "paddleocr-vl:page",
    "glm-ocr:layout", "glm-ocr-4bit:layout", "glm-ocr:page",
]


# ---------------------------------------------------------------- metrics
def norm(text: str) -> str:
    from app.quality import strip_markdown
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)  # YAML front matter = source metadata, not body text
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("−", "-")
    return strip_markdown(text)


def cer_wer(hyp: str, ref: str) -> tuple[float, float]:
    from rapidfuzz.distance import Levenshtein
    h, r = norm(hyp), norm(ref)
    cer = Levenshtein.distance(h, r) / max(1, len(r))
    wer = Levenshtein.distance(h.split(), r.split()) / max(1, len(r.split()))
    return round(cer, 4), round(wer, 4)


def md_rows(md: str) -> set[str]:
    rows = set()
    for line in md.splitlines():
        line = line.strip()
        if line.startswith("|") and not re.match(r"^\|[\s:|-]+\|$", line):
            cells = [norm(c).replace(" ", "") for c in line.strip("|").split("|")]
            rows.add("|".join(cells))
    return rows


def table_score(md: str, gt_rows: list[list[str]]) -> float:
    found = md_rows(md)
    want = ["|".join(norm(c).replace(" ", "") for c in r) for r in gt_rows]
    return round(sum(w in found for w in want) / len(want), 3)


def heading_score(md: str, headings: list[str]) -> float:
    from rapidfuzz import fuzz
    lines = [norm(l) for l in md.splitlines() if l.lstrip().startswith("#")]
    hit = sum(any(fuzz.ratio(norm(h), l) >= 90 for l in lines) for h in headings)
    return round(hit / len(headings), 3)


def expectations(name: str) -> tuple[list[list[str]] | None, list[str]]:
    import make_dataset as ds
    if "long" in name:
        return None, [f"Capitolo {n}" for n in range(1, 13)]
    if "_it" in name or name.startswith("image_it"):
        heads = [ds.IT_TITLE] + [h for h, _ in ds.IT_SECTIONS] + ([h for h, _ in ds.IT_SECTIONS_2] if "image" not in name else [])
        return ds.IT_TABLE, heads
    if "en_twocol" in name or "image_en" in name:
        return ds.EN_TABLE, [ds.EN_TITLE]
    return None, [ds.FORMULA_LINES[0]]


# ---------------------------------------------------------------- engines
def make_engine(name: str):
    from app.resources import EngineResources
    big = EngineResources(4000, 4000, 1)
    if name.startswith("docling"):
        from app.engines.docling_engine import DoclingEngine, page_outputs
        ocr = {"docling": None, "docling-ocrmac": "ocrmac", "docling-rapidocr": "rapidocr"}[name]
        if ocr == "rapidocr":
            return RapidDocling()
        e = DoclingEngine(ocr, big)
        return lambda path, imgs: [o.markdown for o in page_outputs(e.convert(path), e.name, None)], e
    if name.startswith("apple-vision"):
        from app.engines.vision_engine import VisionEngine
        e = VisionEngine()
        return lambda path, imgs: [o.markdown for o in e.convert_images(imgs, list(range(1, len(imgs) + 1)))], e
    if name.startswith("ocrmac"):
        from ocrmac import ocrmac
        fw = name.split("-")[1]

        def run(path, imgs):
            pages = []
            for img in imgs:
                res = ocrmac.OCR(str(img), framework=fw, recognition_level="accurate",
                                 language_preference=["it-IT", "en-US"]).recognize()
                res.sort(key=lambda r: (-round(r[2][1] + r[2][3], 2), r[2][0]))  # top-to-bottom, then left
                pages.append("\n".join(r[0] for r in res))
            return pages
        return run, None
    from app.engines.vlm_engine import VlmEngine
    key, mode = name.split(":")
    e = VlmEngine(key, "glm" if key.startswith("glm") else "paddle", mode, big)
    return lambda path, imgs: [o.markdown for o in e.convert_images(imgs, list(range(1, len(imgs) + 1)))], e


class RapidDocling:
    """Docling + RapidOCR (torch backend: onnxruntime isn't installed and isn't worth adding)."""

    def __new__(cls):
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import OcrMode, PdfPipelineOptions, RapidOcrOptions
        from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption
        opts = PdfPipelineOptions(do_ocr=True, ocr_options=RapidOcrOptions(backend="torch", mode=OcrMode.FULL_PAGE))
        conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts),
                                                 InputFormat.IMAGE: ImageFormatOption(pipeline_options=opts)})

        def run(path, imgs):
            doc = conv.convert(path).document
            return [doc.export_to_markdown(page_no=p) for p in sorted(doc.pages)] or [doc.export_to_markdown()]
        return run, None


def worker(name: str, files: list[str], out: Path) -> None:
    from app import config
    from app.analyzer import render_pages
    from app.resources import footprint, peak_footprint
    base = footprint()
    t = time.perf_counter()
    run, eng = make_engine(name)
    if eng is not None:
        eng.run(lambda m: None)  # force model load so it's timed separately
    load_s = time.perf_counter() - t
    loaded = footprint()
    results = []
    tmp = Path(tempfile.mkdtemp(prefix="bench_"))
    for f in files:
        path = DATA / f
        if path.suffix == ".pdf":
            import pypdfium2 as pdfium
            n = len(pdfium.PdfDocument(path))
            imgs = render_pages(path, list(range(1, n + 1)), tmp / path.stem, config.OCR_DPI)
        else:
            imgs = [path]
        t = time.perf_counter()
        try:
            pages = run(path, imgs)
            err = None
        except Exception as exc:  # record and continue: one failure shouldn't hide the rest
            pages, err = [], f"{type(exc).__name__}: {exc}"
        dt = time.perf_counter() - t
        md = "\n\n".join(pages)
        gt = (DATA / f"{path.stem}.gt.txt").read_text(encoding="utf-8")
        cer, wer = cer_wer(md, gt) if not err else (None, None)
        table, heads = expectations(f)
        results.append(dict(file=f, pages=len(imgs), seconds=round(dt, 2), s_per_page=round(dt / len(imgs), 2),
                            cer=cer, wer=wer, table=table_score(md, table) if table and not err else None,
                            headings=heading_score(md, heads) if not err else None, error=err, markdown=md))
    out.write_text(json.dumps(dict(engine=name, load_s=round(load_s, 2), base_mb=base // 2**20,
                                   model_mb=(loaded - base) // 2**20, peak_mb=peak_footprint() // 2**20,
                                   files=results), ensure_ascii=False, indent=1))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        worker(sys.argv[2], json.loads(sys.argv[3]), Path(sys.argv[4]))
        return
    OUT.mkdir(parents=True, exist_ok=True)
    names = sys.argv[1:] or ENGINES
    for name in names:
        files = SCANS + DIGITAL
        out = OUT / f"engine_{name.replace(':', '_')}.json"
        print(f"== {name}", flush=True)
        t = time.time()
        p = subprocess.run([sys.executable, __file__, "worker", name, json.dumps(files), str(out)],
                           capture_output=True, text=True)
        if p.returncode != 0:
            print(p.stderr[-2000:])
            continue
        r = json.loads(out.read_text())
        ok = [f for f in r["files"] if f["cer"] is not None]
        print(f"   {time.time() - t:.0f}s load={r['load_s']}s model={r['model_mb']}MB peak={r['peak_mb']}MB "
              f"errors={len(r['files']) - len(ok)}", flush=True)
        for f in r["files"]:
            print(f"   {f['file']:32s} cer={f['cer']} wer={f['wer']} table={f['table']} head={f['headings']} "
                  f"{f['s_per_page']}s/p {f['error'] or ''}", flush=True)


if __name__ == "__main__":
    main()
