"""Format coverage beyond the core dataset: TIFF/BMP, legacy Word via textutil, ODT, Markdown,
scanned formulas, charts, and a clear error for formats that need LibreOffice."""

import re
import subprocess
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from PIL import Image

from app import analyzer
from app.pipeline import Context, convert_file

DATA = Path(__file__).resolve().parent.parent / "benchmarks" / "data"


def convert(tmp_path, src: Path, mode="auto"):
    out = tmp_path / "out"
    s = convert_file(src, Context(out_dir=out, work_dir=tmp_path / "work", name=src.name, mode=mode))
    return Path(s["markdown"]).read_text(), s["report"], out


def page_image(pdf: str, n: int = 0, dpi: int = 200) -> Image.Image:
    doc = pdfium.PdfDocument(DATA / pdf)
    try:
        return doc[n].render(scale=dpi / 72).to_pil().convert("RGB")
    finally:
        doc.close()


def test_multipage_tiff(tmp_path):
    src = tmp_path / "scansioni.tiff"
    p1, p2 = page_image("scan_long_it.pdf", 0), page_image("scan_long_it.pdf", 1)
    p1.save(src, save_all=True, append_images=[p2], compression="tiff_lzw")
    md, data, _ = convert(tmp_path, src)
    assert [p["page"] for p in data["pages"]] == [1, 2]
    assert data["analysis"]["page_kinds"] == {"scanned": 2}
    assert "Capitolo 1" in md and "Capitolo 2" in md


def test_bmp(tmp_path):
    src = tmp_path / "nota.bmp"
    page_image("scan_long_it.pdf", 2).save(src)
    md, _, _ = convert(tmp_path, src)
    assert "Capitolo 3" in md


def test_doc_and_odt_via_textutil(tmp_path):
    for fmt in ("doc", "odt"):
        src = tmp_path / f"avviso.{fmt}"
        subprocess.run(["/usr/bin/textutil", "-convert", fmt, "-output", str(src), str(DATA / "pagina.html")], check=True)
        md, _, _ = convert(tmp_path / fmt, src)
        assert "biblioteca" in md and "Orari ridotti" in md


def test_markdown_input(tmp_path):
    src = tmp_path / "note.md"
    src.write_text("# Titolo\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    md, _, _ = convert(tmp_path, src)
    assert "# Titolo" in md and re.search(r"\|\s*1\s*\|\s*2\s*\|", md)


def test_scanned_formulas_go_to_paddle(tmp_path):
    src = tmp_path / "formule.png"
    page_image("digital_formula_en.pdf").save(src)
    md, data, _ = convert(tmp_path, src)
    page = data["pages"][0]
    assert page["engine"].startswith("paddleocr-vl") and page["escalated_from"]["reason"] == "formule"
    assert "$" in md  # formulas come back as LaTeX


def test_docx_chart_exported_as_image(tmp_path):
    md, _, out = convert(tmp_path, DATA / "office_report_it.docx")
    assert [p.name for p in (out / "assets").iterdir()] == ["immagine_001.png"] and "](assets/immagine_001.png)" in md


@pytest.mark.parametrize("ext", [".ppt", ".xls", ".odp", ".ods"])
def test_libreoffice_formats_report_clear_error(tmp_path, ext):
    if analyzer.soffice():
        pytest.skip("LibreOffice installed: format is supported")
    src = tmp_path / f"vecchio{ext}"
    src.write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 512)
    with pytest.raises(ValueError, match="LibreOffice"):
        convert(tmp_path, src)
