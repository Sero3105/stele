"""Format coverage beyond the core dataset: TIFF/BMP, legacy Word via textutil, ODT, Markdown,
scanned formulas, charts, and a clear error for formats that need LibreOffice."""

import io
import json
import re
import subprocess
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from PIL import Image

from app import analyzer
from app.pipeline import Context, convert_file, full_text

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


def test_markdown_note_published_as_written(tmp_path):
    note = ("---\ntags: [lavoro]\n---\n# Nota\n\n> [!note] Attenzione\n> Portare i documenti.\n\n"
            "- [ ] da fare\n- [x] fatto\n\n![web](https://example.com/a.png)\n")
    src = tmp_path / "nota.md"
    src.write_text(note)
    md, _, _ = convert(tmp_path, src)
    assert md == note


def test_linked_images_of_markdown_and_html_are_published(tmp_path):
    up = tmp_path / "upload"
    (up / "img").mkdir(parents=True)
    Image.new("RGB", (80, 60), "red").save(up / "img" / "foto.png")
    (tmp_path / "segreto.png").write_bytes((up / "img" / "foto.png").read_bytes())  # outside the upload
    (up / "nota.md").write_text("Testo\n\n![foto](img/foto.png) ![x](../segreto.png)\n")
    (up / "pagina.html").write_text('<html><body><p>Testo</p><img src="img/foto.png" alt="foto"></body></html>')
    for name in ("nota.md", "pagina.html"):
        out = tmp_path / name
        s = convert_file(up / name, Context(out_dir=out, work_dir=tmp_path / f"w-{name}", name=name,
                                            mode="fast", upload_root=up))
        md = Path(s["markdown"]).read_text()
        assert "![foto](assets/immagine_001.png)" in md and "<!-- image -->" not in md, md
        assert [p.name for p in (out / "assets").iterdir()] == ["immagine_001.png"]
    assert "segreto" not in md


def test_scanned_formulas_go_to_paddle(tmp_path):
    src = tmp_path / "formule.png"
    page_image("digital_formula_en.pdf").save(src)
    md, data, _ = convert(tmp_path, src)
    page = data["pages"][0]
    assert page["engine"].startswith("paddleocr-vl") and page["escalated_from"]["reason"] == "formule"
    assert "$" in md  # formulas come back as LaTeX


def test_docx_chart_exported_as_image(tmp_path):
    md, _, out = convert(tmp_path, DATA / "office_report_it.docx")
    assert sorted(p.name for p in (out / "assets").iterdir()) == ["immagine_001.png", "tabella_001.csv"]
    assert "](assets/immagine_001.png)" in md


@pytest.mark.parametrize("ext", [".ppt", ".xls", ".odp", ".ods"])
def test_libreoffice_formats_report_clear_error(tmp_path, ext):
    if analyzer.soffice():
        pytest.skip("LibreOffice installed: format is supported")
    src = tmp_path / f"vecchio{ext}"
    src.write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 512)
    with pytest.raises(ValueError, match="LibreOffice"):
        convert(tmp_path, src)


def test_digital_display_formulas_become_latex_and_keep_the_text_layer(tmp_path):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table
    pdfmetrics.registerFont(TTFont("ArialUni", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"))
    body = getSampleStyleSheet()["BodyText"]
    eq = body.clone("eq", fontName="ArialUni", fontSize=12, leading=16)

    def formula(text, n):  # a centred display formula with its number on the right
        return Table([[Paragraph(text, eq), Paragraph(f"({n})", eq)]], colWidths=[380, 40])
    intro = ("The total energy of the system is given by the following relation, which we use in every "
             "experiment described in this report. It holds for every closed system considered here.")
    story = [Paragraph(intro, body), Spacer(1, 8), Paragraph(intro, body), Spacer(1, 8),
             formula("E = ½ m v² + m g h", 1), Spacer(1, 8),
             Paragraph("where m is the mass, v the velocity and h the height above the reference level.", body),
             Spacer(1, 8), formula("∫₀¹ x² dx = 1/3", 2)]
    src = tmp_path / "formule.pdf"
    SimpleDocTemplate(str(src), pagesize=A4).build(story)
    md, data, _ = convert(tmp_path, src)
    page = data["pages"][0]
    assert page["engine"] == "docling-ocrmac" and page["formulas"] == 2 and not data["escalated_pages"]
    assert "formula-not-decoded" not in md and md.count("$$") == 4 and "\\[" not in md and "\\int" in md
    assert "where m is the mass, v the velocity and h the height above the reference level." in md  # exact text layer


def test_cli_converts_a_folder(tmp_path, capsys):
    from app.convert import main
    folder = tmp_path / "Cartella"
    (folder / "sub").mkdir(parents=True)
    (folder / "note.txt").write_bytes((DATA / "note.txt").read_bytes())
    (folder / "sub" / "pagina.html").write_bytes((DATA / "pagina.html").read_bytes())
    (folder / "rotto.pdf").write_bytes((DATA / "broken_truncated.pdf").read_bytes())
    (folder / ".DS_Store").write_bytes(b"x")
    out = tmp_path / "risultati"
    assert main([str(folder), "--out", str(out), "--mode", "fast", "--rag"]) == 1  # one file failed
    assert (out / "Cartella/note/note.md").exists() and (out / "Cartella/note/chunks.jsonl").exists()
    assert (out / "Cartella/sub/pagina/pagina.md").exists() and not (out / "Cartella/rotto").exists()
    err = capsys.readouterr().err
    assert "rotto.pdf: errore" in err and ".DS_Store" not in err
    assert main([str(folder / "note.txt"), "--out", str(out / "Cartella")]) == 1  # never overwrites
    assert "esiste già" in capsys.readouterr().err


def test_page_range_and_obsidian_properties(tmp_path):
    out = tmp_path / "out"
    s = convert_file(DATA / "digital_it.pdf", Context(out_dir=out, work_dir=tmp_path / "work", name="digital_it.pdf",
                                                      pages="2"))
    md = Path(s["markdown"]).read_text()
    assert "Introduzione" not in md and "## Conclusioni" in md
    assert s["pages"] == 1 and s["source_pages"] == 2
    assert 'source: "digital_it.pdf"\npages: 1\nlanguage: "it"\n' in md and 'tags: ["stele"]' in md
    assert json.loads((out / "metadata.json").read_text())["page_range"] == "2"
    with pytest.raises(ValueError, match="ha 2 pagine"):
        convert_file(DATA / "digital_it.pdf", Context(out_dir=tmp_path / "o2", work_dir=tmp_path / "w2",
                                                      name="x.pdf", pages="5-9"))


def test_split_into_chapter_notes(tmp_path):
    out = tmp_path / "out"
    s = convert_file(DATA / "digital_it.pdf", Context(out_dir=out, work_dir=tmp_path / "work", name="digital_it.pdf",
                                                      split=True))
    index = Path(s["markdown"]).read_text()
    notes = sorted(p.name for p in (out / "capitoli").iterdir())
    assert s["chapters"] == 4 and notes == ["digital_it - 01 Introduzione.md", "digital_it - 02 Metodologia.md",
                                            "digital_it - 03 Risultati.md", "digital_it - 04 Conclusioni.md"]
    assert "# Relazione tecnica sul consumo energetico" in index and "chapters: 4" in index
    assert "1. [[digital_it - 01 Introduzione|Introduzione]]" in index and "Questa relazione" not in index
    first = (out / "capitoli" / notes[0]).read_text()
    assert first.startswith('---\ntitle: "Introduzione"') and 'up: "[[digital_it]]"' in first and "# Introduzione" in first
    pictures = [n for n in notes if "](../assets/" in (out / "capitoli" / n).read_text()]
    assert pictures and all((out / "assets").glob("immagine_*"))  # chapter links reach the shared assets/
    full = full_text(out)
    assert full.index("## Capitoli") < full.index("Questa relazione") < full.index("# Conclusioni")


def make_epub(path: Path, encrypted: bool = False) -> None:
    import zipfile
    from PIL import Image
    img = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(img, "PNG")
    page = ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{t}</title>'
            '</head><body><h1>{t}</h1><p>{p}</p>{x}</body></html>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile '
                   'full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        if encrypted:
            z.writestr("META-INF/encryption.xml", "<encryption/>")
        z.writestr("OEBPS/content.opf", '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Il libro di prova</dc:title>'
                   '<dc:creator>Ada Rossi</dc:creator><dc:date>2021-05-04</dc:date></metadata><manifest>'
                   '<item id="c2" href="Text/due.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="c1" href="Text/uno.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="im" href="Images/rosso.png" media-type="image/png"/></manifest>'
                   '<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>')
        z.writestr("OEBPS/Text/uno.xhtml", page.format(t="Primo capitolo", p="La storia comincia in una piccola città.",
                                                       x='<p><img src="../Images/rosso.png" alt="Un quadrato rosso"/></p>'))
        z.writestr("OEBPS/Text/due.xhtml", page.format(t="Secondo capitolo", p="La storia continua al mare.", x=""))
        z.writestr("OEBPS/Images/rosso.png", img.getvalue())


def test_epub_chapters_in_reading_order_with_pictures(tmp_path):
    src = tmp_path / "libro.epub"
    make_epub(src)
    md, data, out = convert(tmp_path, src)
    assert md.startswith('---\ntitle: "Il libro di prova"\nauthor: "Ada Rossi"\ndate: "2021-05-04"')
    assert md.index("Primo capitolo") < md.index("piccola città") < md.index("Secondo capitolo") < md.index("al mare")
    assert "![Un quadrato rosso](assets/immagine_001.png)" in md and (out / "assets/immagine_001.png").exists()
    drm = tmp_path / "drm.epub"
    make_epub(drm, encrypted=True)
    with pytest.raises(ValueError, match="DRM"):
        convert(tmp_path / "x", drm)


def make_email(path: Path) -> None:
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = "Bolletta di ottobre", "Ufficio <ufficio@example.com>", "me@example.com"
    msg["Date"] = "Mon, 13 Oct 2025 09:30:00 +0200"
    msg.set_content("Testo semplice.")
    msg.add_alternative("<html><body><p>Gentile cliente, ecco i consumi.</p><table><tr><th>Mese</th><th>kWh</th></tr>"
                        "<tr><td>Ottobre</td><td>320</td></tr></table></body></html>", subtype="html")
    msg.add_attachment((DATA / "note.txt").read_bytes(), maintype="text", subtype="plain", filename="dettaglio.txt")
    path.write_bytes(bytes(msg))


def test_email_header_html_body_and_attachments(tmp_path):
    src = tmp_path / "bolletta.eml"
    make_email(src)
    ctx = Context(out_dir=tmp_path / "out", work_dir=tmp_path / "work", name=src.name)
    s = convert_file(src, ctx)
    md = Path(s["markdown"]).read_text()
    assert md.startswith('---\ntitle: "Bolletta di ottobre"\nauthor: "Ufficio <ufficio@example.com>"\ndate: "2025-10-13"')
    assert "# Bolletta di ottobre" in md and "**Da:** Ufficio <ufficio@example.com>" in md
    assert "| Ottobre | 320 |" in md and "## Allegati\n\n- dettaglio.txt" in md and "<!--" not in md
    assert [Path(a).name for a in s["attachments"]] == ["dettaglio.txt"]
    assert Path(s["attachments"][0]).read_bytes() == (DATA / "note.txt").read_bytes()


def test_cli_converts_email_attachments_too(tmp_path):
    from app.convert import main
    src = tmp_path / "in" / "bolletta.eml"
    src.parent.mkdir()
    make_email(src)
    out = tmp_path / "out"
    assert main([str(src), "--out", str(out), "--mode", "fast"]) == 0
    assert (out / "bolletta/bolletta.md").exists()
    assert (out / "bolletta (allegati)/dettaglio/dettaglio.md").exists()
