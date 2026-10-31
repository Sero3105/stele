"""Integration tests: real engines on the generated dataset (needs models + native helper)."""

import json
from pathlib import Path

import pytest

from app import router
from app.pipeline import Cancelled, Context, convert_file

DATA = Path(__file__).resolve().parent.parent / "benchmarks" / "data"


def run(tmp_path, name, mode=router.AUTO, **kw):
    ctx = Context(out_dir=tmp_path / "out", work_dir=tmp_path / "work", name=name, mode=mode, **kw)
    summary = convert_file(DATA / name, ctx)
    md = Path(summary["markdown"]).read_text()
    return summary, md, summary["report"]


def published(tmp_path) -> set[str]:
    out = tmp_path / "out"
    return {str(p.relative_to(out)) for p in out.rglob("*")}


def test_digital_pdf_uses_text_layer_only(tmp_path):
    s, md, data = run(tmp_path, "digital_it.pdf")
    assert s["engines"] == ["docling-ocrmac"] and not s["escalated_pages"]
    assert "Relazione tecnica sul consumo energetico" in md and "## Metodologia" in md
    assert "| Scuola Rossi" in md
    assert md.index("Metodologia") < md.index("Risultati") < md.index("Conclusioni")  # page 1 before page 2
    assert "<!--" not in md                                                             # no engine/page comments
    # One document folder, nothing intermediate: the chart picture is a linked asset.
    assert published(tmp_path) == {"digital_it.md", "metadata.json", "assets", "assets/immagine_001.png"}
    assert "](assets/immagine_001.png)" in md
    meta = json.loads((tmp_path / "out" / "metadata.json").read_text())
    assert meta["source"] == "digital_it.pdf" and meta["pages"] == 2 and meta["mode"] == "auto"
    assert md.startswith('---\ntitle: "Relazione tecnica sul consumo energetico"')  # from the PDF's own metadata


def test_scanned_table_page_goes_to_paddle(tmp_path):
    s, md, data = run(tmp_path, "scan_clean300_it.pdf")
    p1 = data["pages"][0]
    assert p1["engine"].startswith("paddleocr-vl") and "tabelle" in p1["escalated_from"]["reason"]
    assert "| Scuola Rossi | 2450 | 182300 | 74,4 |" in md


def test_fast_mode_uses_only_light_engines(tmp_path):
    s, md, _ = run(tmp_path, "scan_noisy150_en_twocol.pdf", router.FAST)
    assert all(e in ("apple-vision", "docling-ocrmac") for e in s["engines"])
    assert "Coastal erosion is measured" in md


def test_mixed_pdf_keeps_page_order_and_routes_per_page(tmp_path):
    s, md, data = run(tmp_path, "mixed_digital_scan.pdf")
    engines = [p["engine"] for p in data["pages"]]
    assert [p["page"] for p in data["pages"]] == [1, 2, 3, 4]
    assert engines[0] == engines[1] == engines[3] == "docling-ocrmac" and engines[2] != "docling-ocrmac"
    assert md.index("Relazione tecnica") < md.index("Coastal erosion") < md.index("Equations used")
    # Several engines, still one document: each piece of text exactly once.
    assert md.count("Coastal erosion is measured") == 1 and md.count("Field Notes on Coastal Erosion") == 1
    assert {p.split("/")[0] for p in published(tmp_path)} == {"mixed_digital_scan.md", "metadata.json", "assets"}


def test_image_input(tmp_path):
    s, md, _ = run(tmp_path, "image_en_photo.jpg")
    assert "Field Notes on Coastal Erosion Monitoring" in md


def test_pptx_slides_and_notes(tmp_path):
    _, md, _ = run(tmp_path, "office_slides_en.pptx")
    assert "## Slide 1" in md and "## Slide 3" in md
    assert "### Key findings" in md  # slide titles nested under their slide
    assert "Note del relatore:** Speaker note: thank the volunteers." in md
    assert "| North" in md


def test_xlsx_sheets_formulas(tmp_path):
    _, md, data = run(tmp_path, "office_data.xlsx")
    assert "## Foglio: Consumi" in md and "## Foglio: Spiagge" in md
    assert "`B7`: `=SUM(B3:B6)`" in md
    meta = json.loads((tmp_path / "out" / "metadata.json").read_text())
    assert [s["name"] for s in meta["sheets"]] == ["Consumi", "Spiagge"] and meta["sheets"][0]["merged_cells"] == ["A1:D1"]
    assert published(tmp_path) == {"office_data.md", "metadata.json"}


def test_docx(tmp_path):
    _, md, _ = run(tmp_path, "office_report_it.docx")
    assert "# Relazione tecnica" in md and "- Sostituzione delle caldaie" in md and "1. Primo passo" in md


@pytest.mark.parametrize("name,needle", [("note.txt", "Ricontrollare i contatori"),
                                          ("pagina.html", "# Avviso"), ("dati.csv", "| Scuola Rossi")])
def test_text_formats(tmp_path, name, needle):
    _, md, _ = run(tmp_path, name)
    assert needle in md


@pytest.mark.parametrize("name", ["broken_truncated.pdf", "empty.pdf", "unsupported.xyz"])
def test_bad_files_raise_clean_errors(tmp_path, name):
    with pytest.raises(ValueError):
        run(tmp_path, name)


def test_cancellation(tmp_path):
    with pytest.raises(Cancelled):
        run(tmp_path, "scan_noisy150_it.pdf", cancelled=lambda: True)


def test_legacy_rtf_via_textutil(tmp_path):
    import subprocess
    rtf = tmp_path / "lettera.rtf"
    subprocess.run(["/usr/bin/textutil", "-convert", "rtf", "-output", str(rtf), str(DATA / "pagina.html")], check=True)
    ctx = Context(out_dir=tmp_path / "out", work_dir=tmp_path / "work", name="lettera.rtf")
    convert_file(rtf, ctx)
    assert "biblioteca" in (tmp_path / "out" / "lettera.md").read_text()


def test_special_characters_in_file_name(tmp_path):
    import shutil
    src = tmp_path / "Relazione è #1 (copia).pdf"
    shutil.copy(DATA / "digital_en_twocol.pdf", src)
    ctx = Context(out_dir=tmp_path / "out", work_dir=tmp_path / "work", name=src.name)
    s = convert_file(src, ctx)
    assert Path(s["markdown"]).name == "Relazione è _1 (copia).md"
    assert json.loads((tmp_path / "out" / "metadata.json").read_text())["source"] == "Relazione è #1 (copia).pdf"


def test_work_files_stay_internal(tmp_path):
    run(tmp_path, "scan_clean300_it.pdf")
    work = {p.name for p in (tmp_path / "work").iterdir()}
    assert "pages" not in work and "assets" not in work  # checkpoints/assets cleaned after publishing
    assert "report.json" in work                          # internal report kept out of the result


def test_image_links_outside_the_document_are_never_read(tmp_path):
    src = tmp_path / "nota.txt"
    src.write_text("Nota\n\n![segreto](/etc/hosts)\n\n![su](../../../../etc/hosts)\n", encoding="utf-8")
    ctx = Context(out_dir=tmp_path / "out", work_dir=tmp_path / "work", name=src.name)
    md = Path(convert_file(src, ctx)["markdown"]).read_text()
    assert "assets" not in published(tmp_path) and "hosts" not in md and "segreto" in md
