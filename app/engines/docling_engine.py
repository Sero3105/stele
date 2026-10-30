"""Docling engines.

One DocumentConverter serves every format. For PDFs/images it runs Docling layout +
TableFormer with OcrMac (Apple Vision via pyobjc) in DEFAULT mode: OCR touches only bitmap
regions, so digital pages keep their exact text layer and scanned pages get OCR in the
same pass (benchmarks/REPORT.md). One instance = layout/table weights loaded once.
"""

from __future__ import annotations

import gc
import math
import logging
from pathlib import Path

from app.engines.base import Engine, PageOutput
from app.resources import EngineResources

log = logging.getLogger(__name__)


def _converter(ocr: str | None, langs: list[str]):
    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import OcrMacOptions, OcrMode, PdfPipelineOptions, TableStructureOptions
    from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

    opts = PdfPipelineOptions(
        do_ocr=ocr is not None,
        do_table_structure=True,
        table_structure_options=TableStructureOptions(do_cell_matching=True),
        generate_picture_images=True,  # pictures are exported as files next to the Markdown
        images_scale=1.5,
        accelerator_options=AcceleratorOptions(device="auto", num_threads=4),
        document_timeout=600,
    )
    if ocr == "ocrmac":
        opts.ocr_options = OcrMacOptions(lang=langs, mode=OcrMode.DEFAULT, recognition="accurate")
    fmt = {InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    if ocr:
        fmt[InputFormat.IMAGE] = ImageFormatOption(pipeline_options=opts)
    return DocumentConverter(format_options=fmt)


class DoclingEngine(Engine):
    def __init__(self, ocr: str | None = None, resources: EngineResources | None = None):
        super().__init__()
        self.ocr = ocr
        self.name = "docling" if ocr is None else f"docling-{ocr}"
        self.resources = resources or EngineResources(1200, 600, 1)

    def _load(self):
        conv = _converter(self.ocr, ["it-IT", "en-US"])
        from docling.datamodel.base_models import InputFormat
        conv.initialize_pipeline(InputFormat.PDF)
        return conv

    def _unload(self, conv) -> None:
        del conv
        gc.collect()
        try:
            import torch
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:  # torch absent or MPS unavailable: nothing to free
            pass

    def convert(self, path: Path, pages: list[int] | None = None, working_mb: int | None = None, cancelled=lambda: False):
        """Return the DoclingDocument for `path` (optionally only some 1-based pages)."""
        def work(conv):
            kw = {}
            if pages:
                kw["page_range"] = (min(pages), max(pages))
            res = conv.convert(path, raises_on_error=False, **kw)
            if res.status.name not in ("SUCCESS", "PARTIAL_SUCCESS"):
                errs = "; ".join(e.error_message for e in res.errors) or res.status.name
                raise RuntimeError(f"Docling: {errs}")
            return res
        return self.run(work, working_mb, cancelled)


def _markdown(doc, page_no: int | None, assets: Path | None) -> str:
    from docling_core.types.doc.base import CaptionPlacement
    # LAYOUT: captions where they are on the page (a figure caption below its picture stays below).
    if assets is None:
        return doc.export_to_markdown(page_no=page_no, caption_placement=CaptionPlacement.LAYOUT)
    from docling_core.types.doc import ImageRefMode
    # save_as_markdown is the public API that writes picture files; links are made relative after.
    tmp = assets.parent / f".{assets.name}.{page_no or 0}.md"
    doc.save_as_markdown(tmp, artifacts_dir=assets, image_mode=ImageRefMode.REFERENCED, page_no=page_no,
                         caption_placement=CaptionPlacement.LAYOUT)
    try:
        return tmp.read_text(encoding="utf-8").replace(str(assets), assets.name)
    finally:
        tmp.unlink(missing_ok=True)


def page_outputs(res, engine: str, pages: list[int] | None, assets: Path | None = None) -> list[PageOutput]:
    """Split a Docling result into per-page Markdown (page_no=0 for page-less formats).

    With `assets`, pictures are written there as PNG files and linked from the Markdown."""
    doc = res.document
    conf = getattr(res, "confidence", None)
    nums = sorted(doc.pages) if doc.pages else []
    if not nums or doc.pages and pages is None and _single_flow(res):
        return [PageOutput(0, _markdown(doc, None, assets), engine)]
    out = []
    for p in (n for n in nums if pages is None or n in pages):
        data = {}
        if conf is not None and p in conf.pages:  # keyed by 1-based page number
            pc = conf.pages[p]
            data["docling_confidence"] = {k: _f(getattr(pc, k, None)) for k in
                                          ("parse_score", "layout_score", "ocr_score", "table_score")}
        out.append(PageOutput(p, _markdown(doc, p, assets), engine, data=data))
    return out


def _single_flow(res) -> bool:
    """DOCX/HTML/MD have no real pages (Docling may still create one): export them whole."""
    return res.input.format.value in ("docx", "html", "md", "csv", "asciidoc")


def _f(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else round(v, 3)
