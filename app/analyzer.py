"""File and per-page analysis: what is this document and what does each page need?"""

from __future__ import annotations

import mimetypes
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

import filetype

from app.quality import guess_language, text_quality

PDF = {".pdf"}
IMAGE = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
OFFICE = {".docx", ".pptx", ".xlsx", ".xlsm"}
# textutil (built into macOS) turns these into DOCX without LibreOffice.
TEXTUTIL = {".doc", ".rtf", ".odt"}
LIBREOFFICE_ONLY = {".ppt", ".xls", ".odp", ".ods"}
MARKUP = {".html", ".htm", ".md", ".csv"}
PLAIN = {".txt"}


@dataclass
class PageInfo:
    number: int
    kind: str              # digital | scanned | hybrid | blank
    chars: int
    image_cover: float     # share of the page covered by raster images
    text_quality: float    # quality of the embedded text layer (digital/hybrid)


@dataclass
class FileInfo:
    path: str
    ext: str
    mime: str | None
    size: int
    family: str            # pdf | image | office | legacy | markup | text | unsupported
    page_count: int = 0
    pages: list[PageInfo] = field(default_factory=list)
    language: str | None = None
    error: str | None = None
    note: str | None = None

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for p in self.pages:
            c[p.kind] = c.get(p.kind, 0) + 1
        return c

    def as_dict(self) -> dict:
        d = asdict(self)
        d["page_kinds"] = self.counts()
        return d


def soffice() -> str | None:
    return shutil.which("soffice") or (
        "/Applications/LibreOffice.app/Contents/MacOS/soffice"
        if Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").exists() else None)


def classify_page(chars: int, image_cover: float, quality: float) -> str:
    if chars < 15:
        return "scanned" if image_cover > 0.05 else "blank"
    if quality < 0.5:
        return "scanned"  # broken text layer (bad encoding, invisible junk): OCR it
    if image_cover > 0.6:
        return "hybrid"   # text layer over a big image (e.g. OCRed scan) or text + large figure
    return "digital"


def analyze_pdf(path: Path, info: FileInfo) -> None:
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw
    from docling.utils.locks import pypdfium2_lock  # pdfium is not thread-safe; share docling's lock

    sample = []
    with pypdfium2_lock:
        doc = pdfium.PdfDocument(path)
        try:
            info.page_count = len(doc)
            for i in range(len(doc)):
                page = doc[i]
                w, h = page.get_size()
                tp = page.get_textpage()
                text = tp.get_text_range()
                cover = 0.0
                for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE], max_depth=3):
                    x0, y0, x1, y1 = obj.get_bounds()
                    cover += max(0, min(x1, w) - max(x0, 0)) * max(0, min(y1, h) - max(y0, 0))
                cover = min(1.0, cover / (w * h)) if w * h else 0.0
                chars = len(text.strip())
                q = text_quality(text) if chars >= 15 else 0.0
                info.pages.append(PageInfo(i + 1, classify_page(chars, cover, q), chars, round(cover, 3), q))
                if len(sample) < 5:
                    sample.append(text)
                tp.close()
                page.close()
        finally:
            doc.close()
    info.language = guess_language(" ".join(sample))


def analyze(path: Path) -> FileInfo:
    ext = path.suffix.lower()
    size = path.stat().st_size
    kind = filetype.guess(str(path)) if size else None
    mime = kind.mime if kind else mimetypes.guess_type(path.name)[0]
    info = FileInfo(str(path), ext, mime, size, "unsupported")
    if size == 0:
        info.error = "File vuoto"
        return info
    if ext in PDF:
        info.family = "pdf"
        try:
            analyze_pdf(path, info)
        except Exception as exc:
            info.error = f"PDF illeggibile o danneggiato: {exc}"
    elif ext in IMAGE:
        info.family, info.page_count = "image", 1
        try:
            from PIL import Image
            with Image.open(path) as im:
                im.verify()
            info.pages = [PageInfo(1, "scanned", 0, 1.0, 0.0)]
        except Exception as exc:
            info.error = f"Immagine non valida: {exc}"
    elif ext in OFFICE:
        info.family = "office"
    elif ext in TEXTUTIL:
        info.family = "legacy"
    elif ext in LIBREOFFICE_ONLY:
        info.family = "legacy"
        if not soffice():
            info.error = f"Il formato {ext} richiede LibreOffice (brew install --cask libreoffice)"
    elif ext in MARKUP:
        info.family = "markup"
    elif ext in PLAIN:
        info.family = "text"
    else:
        info.error = f"Formato non supportato: {ext or 'senza estensione'}"
    return info


def render_pages(pdf: Path, pages: list[int], out_dir: Path, dpi: int) -> list[Path]:
    """Rasterise 1-based `pages` to PNG files (one page in memory at a time)."""
    import pypdfium2 as pdfium
    from docling.utils.locks import pypdfium2_lock

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for n in pages:
        with pypdfium2_lock:
            doc = pdfium.PdfDocument(pdf)
            try:
                img = doc[n - 1].render(scale=dpi / 72, grayscale=False).to_pil()
            finally:
                doc.close()
        p = out_dir / f"page_{n:05d}.png"
        img.save(p)
        img.close()
        paths.append(p)
    return paths
