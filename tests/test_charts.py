"""Chart data extraction: kept only when every value is printed in the image."""

import sys
from pathlib import Path

from app.charts import alt_text, verify_chart_table
from app.pipeline import Context, convert_file

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "benchmarks" / "data"
sys.path.insert(0, str(ROOT / "benchmarks"))

# Real PaddleOCR-VL 1.6 output on the test chart: invented Spanish header, misspelled label.
RAW = "Categoría | Consumo annuo per edificio (MWh)\nScuola | 182\nMunicipio | 252\nPalstra | 96\nBiblioteca | 41"
# Real Apple Vision lines read from the same image.
VISION = ["Consumo annuo per edificio (MWh)", "252", "182", "96", "41", "Scuola", "Municipio", "Palestra", "Biblioteca"]


def test_verified_table_uses_printed_text_and_drops_invented_header():
    rows, reason = verify_chart_table(RAW, VISION)
    assert reason == "verificata"
    assert rows == [["", "Consumo annuo per edificio (MWh)"], ["Scuola", "182"], ["Municipio", "252"],
                    ["Palestra", "96"], ["Biblioteca", "41"]]


def test_value_not_printed_rejects_whole_table():
    rows, reason = verify_chart_table(RAW.replace("| 96", "| 97"), VISION)
    assert rows is None and "97" in reason


def test_label_not_printed_rejects_whole_table():
    rows, reason = verify_chart_table(RAW.replace("Biblioteca", "Ospedale"), VISION)
    assert rows is None and "Ospedale" in reason


def test_values_estimated_from_bars_are_rejected():
    rows, _ = verify_chart_table(RAW, ["Consumo annuo per edificio (MWh)", "Scuola", "Municipio", "Palestra", "Biblioteca"])
    assert rows is None  # no number printed in the image: nothing can be verified


def test_alt_text():
    assert alt_text(["Titolo", "", "12"]) == "Titolo · 12"
    assert len(alt_text(["x" * 500])) == 200


def test_chart_in_pdf_gets_verified_data_table(tmp_path):
    import make_dataset as ds
    s = convert_file(DATA / "digital_chart_it.pdf",
                     Context(out_dir=tmp_path / "out", work_dir=tmp_path / "work", name="grafico.pdf"))
    md = Path(s["markdown"]).read_text()
    assert "](assets/grafico_001.png)" in md and (tmp_path / "out" / "assets" / "grafico_001.png").exists()
    for name, value in ds.CHART_DATA:
        assert f"| {name} | {value} |" in md
    assert "Categoría" not in md and "Figura 1: consumo annuo per edificio." in md   # caption kept
    assert md.index("grafico_001.png") < md.index("| Scuola | 182 |") < md.index("Figura 1")


def test_chart_without_printed_values_stays_an_image(tmp_path):
    s = convert_file(DATA / "digital_it.pdf",
                     Context(out_dir=tmp_path / "out", work_dir=tmp_path / "work", name="r.pdf"))
    md = Path(s["markdown"]).read_text()
    assert "](assets/immagine_001.png)" in md and "dati letti dal grafico" not in md


def test_fast_mode_skips_chart_extraction(tmp_path):
    s = convert_file(DATA / "digital_chart_it.pdf",
                     Context(out_dir=tmp_path / "out", work_dir=tmp_path / "work", name="g.pdf", mode="fast"))
    assert "| Scuola | 182 |" not in Path(s["markdown"]).read_text()
