"""Table structure across engines: GLM fallback reconstruction (real degraded-page outputs)."""

import json
import re
from pathlib import Path

from app.markdown import assemble
from app.tables import restore_tables

FIX = json.loads((Path(__file__).parent / "fixtures_poor_page.json").read_text())


def nums(s):
    return re.findall(r"\d[\d.,]*\d|\d", s)


def test_glm_lines_rebuilt_with_layout_structure_real_page():
    md, notes = restore_tables(FIX["glm_md"], FIX["paddle_blocks"])
    assert "| Edificio | Superficie (m³) | Consumo (kWh) | Intensità |" in md   # GLM's own header words
    assert "| Scuola Rossi | 2450 | 182300 | 74,4 |" in md                      # GLM's values (Paddle misread them)
    assert "| Palestra Nord | 1800 | 96400 | 53,6 |" in md
    assert "Soula" not in md and "Palestina" not in md                          # Paddle's errors not imported
    assert sorted(nums(md)) == sorted(nums(FIX["glm_md"]))                     # no number lost or added
    assert "ricostruita (4 righe)" in notes[0]


def test_glm_table_already_structured_is_kept():
    glm = "Testo.\n\n| A | B |\n|---|---|\n| x | 1 |"
    blocks = [{"task": "table", "text": "| A | B |\n|---|---|\n| y | 2 |"}]
    md, notes = restore_tables(glm, blocks)
    assert "| x | 1 |" in md and "| y | 2 |" not in md and notes == ["tabella già strutturata nel testo"]


def test_layout_table_used_when_lines_unsplittable_and_numbers_confirmed():
    glm = "Introduzione.\n\nNord ovest 12 Sud\n\nCentro 7 Est\n\nFine."
    blocks = [{"task": "table", "text": "| Zona | N | Lato |\n|---|---|---|\n| Nord ovest | 12 | Sud |\n| Centro | 7 | Est |"}]
    md, notes = restore_tables(glm, blocks)
    assert "| Nord ovest | 12 | Sud |" in md and "Nord ovest 12 Sud" not in md
    assert md.startswith("Introduzione.") and md.endswith("Fine.")


def test_unconfirmed_layout_table_never_overrides_text():
    glm = "Nord ovest 12 Sud\n\nCentro 7 Est"
    blocks = [{"task": "table", "text": "| Zona | N | Lato |\n|---|---|---|\n| Nord ovest | 13 | Sud |\n| Centro | 7 | Est |"}]
    md, notes = restore_tables(glm, blocks)   # 13 is not in the GLM text: can't be trusted
    assert md == "Nord ovest 12 Sud\n\nCentro 7 Est" and "non ricostruibile" in notes[0]


def test_no_header_split_keeps_glm_header_line():
    glm = "Tabella dei consumi annuali per edificio\n\nScuola 2450 182300\n\nMunicipio 3120 251900"
    blocks = [{"task": "table", "text": "| Edificio | Superficie | Consumo |\n|---|---|---|\n| Scuola | 2450 | 182300 |\n| Municipio | 3120 | 251900 |"}]
    md, _ = restore_tables(glm, blocks)
    assert md.startswith("Tabella dei consumi annuali per edificio\n\n| Edificio | Superficie | Consumo |")
    assert "| Municipio | 3120 | 251900 |" in md


def test_table_split_over_pages_after_reconstruction():
    p1, _ = restore_tables("Scuola 2450 182300\n\nMunicipio 3120 251900",
                           [{"task": "table", "text": "| Edificio | m² | kWh |\n|---|---|---|\n| S | 1 | 2 |\n| M | 3 | 4 |"}])
    p2 = "| Edificio | m² | kWh |\n|---|---|---|\n| Palestra | 1800 | 96400 |"
    md = assemble([p1, p2])
    assert md.count("| Edificio | m² | kWh |") == 1 and md.index("Municipio") < md.index("Palestra")
