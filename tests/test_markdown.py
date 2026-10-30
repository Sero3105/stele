"""Document assembly and Markdown normalisation (app/markdown.py): pure, engine-independent."""

import re

from app.markdown import assemble, front_matter, image_refs, parse, rewrite_images


def words(s: str) -> list[str]:
    return re.findall(r"[^\W_]+", s.lower())


def test_single_page_normalisation():
    md = assemble(["# Titolo\n\n\n\nTesto  della\nprima riga.\n• uno\n• due\n1) primo\n2) secondo\n"
                   "| A | B |\n|---|---|\n| 1 |\n"])
    assert md == ("# Titolo\n\nTesto della prima riga.\n\n- uno\n- due\n1. primo\n2. secondo\n\n"
                  "| A | B |\n|---|---|\n| 1 |  |\n")


def test_pages_in_order_without_duplicates():
    pages = [f"## Sezione {n}\n\nContenuto della pagina {n}." for n in range(1, 31)]
    md = assemble(pages)
    positions = [md.index(f"Contenuto della pagina {n}.") for n in range(1, 31)]
    assert positions == sorted(positions)
    assert all(md.count(f"Contenuto della pagina {n}.") == 1 for n in range(1, 31))
    assert "<!--" not in md  # no engine/page comments leak into the document


def test_paragraph_split_across_pages_is_rejoined():
    md = assemble(["Il comune ha avviato un programma di manuten-", "zione degli edifici e delle", "scuole.\n\nNuovo paragrafo."])
    assert "Il comune ha avviato un programma di manutenzione degli edifici e delle scuole." in md
    assert "\n\nNuovo paragrafo." in md


def test_paragraph_ending_a_sentence_is_not_merged():
    md = assemble(["Fine della frase.", "Inizio nuovo paragrafo."])
    assert md == "Fine della frase.\n\nInizio nuovo paragrafo.\n"


def test_table_continued_on_next_page_with_repeated_header():
    p1 = "| Edificio | kWh |\n|---|---|\n| Scuola | 182300 |"
    p2 = "| Edificio | kWh |\n|---|---|\n| Municipio | 251900 |\n\nTesto dopo."
    md = assemble([p1, p2])
    assert md.count("| Edificio | kWh |") == 1
    assert "| Scuola | 182300 |\n| Municipio | 251900 |" in md


def test_table_continued_without_header_keeps_every_row():
    p1 = "| Edificio | kWh |\n|---|---|\n| Scuola | 182300 |"
    p2 = "| Municipio | 251900 |\n|---|---|\n| Palestra | 96400 |"
    md = assemble([p1, p2])
    assert "| Scuola | 182300 |\n| Municipio | 251900 |\n| Palestra | 96400 |" in md


def test_tables_with_different_columns_stay_separate():
    md = assemble(["| A | B |\n|---|---|\n| 1 | 2 |", "| X | Y | Z |\n|---|---|---|\n| 3 | 4 | 5 |"])
    assert "| 1 | 2 |\n\n| X | Y | Z |" in md and md.count("| --- |") == 0


def test_page_numbers_and_running_headers_removed_content_kept():
    pages = [f"Relazione annuale 2025\n\nCorpo della pagina {n}.\n\n{n}" for n in range(1, 6)]
    md = assemble(pages)
    assert md.count("Relazione annuale 2025") == 1           # running header kept once
    assert all(f"Corpo della pagina {n}." in md for n in range(1, 6))
    assert not re.search(r"^\d$", md, re.M)                   # bare page numbers gone


def test_page_number_labels_and_years_on_cover():
    md = assemble(["2025\n\n# Bilancio\n\nTesto.", "Pagina 2 di 3\n\nAltro testo.", "Ancora testo.\n\nPage 3 of 3"])
    assert md.startswith("2025\n\n# Bilancio")              # a year on the cover is content
    assert "Pagina 2 di 3" not in md and "Page 3 of 3" not in md


def test_numbers_inside_content_are_kept():
    md = assemble(["Intro.\n\n42\n\nIl valore sopra è la risposta.\n\nFine."])
    assert "\n42\n" in md


def test_different_chapter_headings_are_not_running_headers():
    pages = [f"## Capitolo {n}\n\nTesto del capitolo {n}." for n in range(1, 8)]
    md = assemble(pages)
    assert all(f"## Capitolo {n}" in md for n in range(1, 8))


def test_heading_hierarchy():
    md = assemble(["# Titolo\n\n### Sotto\n\nx", "# Titolo\n\n## Sezione\n\n#### Dettaglio\n\ny"])
    assert re.findall(r"^#+ .*$", md, re.M) == ["# Titolo", "## Sotto", "## Sezione", "### Dettaglio"]


def test_formulas_and_images_preserved():
    md = assemble(["Testo.\n$$ E = mc^2 $$\n![grafico](assets/grafico_001.png)\nDopo."])
    assert "$$\nE = mc^2\n$$" in md and "\n\n![grafico](assets/grafico_001.png)\n\n" in md


def test_code_blocks_untouched():
    code = "```\n•  non è un elenco\n| non è | una tabella |\n```"
    assert code in assemble([code])


def test_special_characters_preserved():
    text = "Città, perché, €, ±, √, π, “virgolette”, «caporali», 50 °C, H₂O."
    assert text in assemble([text])


def test_front_matter_only_existing_keys():
    assert front_matter({"title": "Relazione", "author": "", "date": None}) == '---\ntitle: "Relazione"\n---\n\n'
    assert front_matter({}) == ""
    assert assemble(["Testo."], {"author": 'Mario "M" Rossi'}).startswith('---\nauthor: "Mario \\"M\\" Rossi"\n---\n')


def test_rewrite_images():
    md = "![a](assets/x.png) e ![b](assets/y.png)"
    assert image_refs(md) == ["assets/x.png", "assets/y.png"]
    assert rewrite_images(md, {"assets/x.png": "assets/immagine_001.png", "assets/y.png": None}) == \
        "![a](assets/immagine_001.png) e b"


def test_no_words_lost_on_real_engine_outputs():
    """Every word of every page survives assembly (page furniture aside) on outputs shaped like
    each engine's: Docling, Vision, PaddleOCR-VL blocks, GLM plain lines."""
    pages = [
        "## Relazione tecnica\n\nQuesta relazione descrive il consumo.\n\n| Edificio | Superficie (m²) |\n|---|---|\n| Scuola | 2450 |",
        "# Titolo\n\nPrimo paragrafo letto da Vision.\n\n- elemento\n- secondo",
        "## Metodologia\n\nPer ciascun edificio abbiamo calcolato\n\n$$\nE = \\frac{1}{2} m v^2\n$$",
        "Riga uno di GLM\n\nRiga due di GLM\n\nEdificio Superficie (m²) Consumo",
    ]
    md = assemble(pages)
    for p in pages:
        for w in words(p):
            assert w in words(md), w


def test_parse_kinds():
    kinds = [b.kind for b in parse("# T\n\ntesto\n\n- a\n\n| x |\n\n![i](a.png)\n\n$$\nx\n$$\n\n<!-- c -->")]
    assert kinds == ["heading", "paragraph", "list", "table", "image", "math", "comment"]


def test_visual_lines_from_ocr_rejoined_into_paragraphs():
    glm = "La palestra mostra i valori più bassi. Il\n\nmunicipio, invece, presenta l'intensità più elevata.\n\n" \
          "- Prima voce.\n\n- Seconda voce.\n\nNuovo paragrafo."
    md = assemble([glm])
    assert "La palestra mostra i valori più bassi. Il municipio, invece, presenta l'intensità più elevata." in md
    assert "- Prima voce.\n- Seconda voce." in md
    assert md.endswith("\n\nNuovo paragrafo.\n")


def test_separate_sentences_stay_separate_paragraphs():
    md = assemble(["Primo paragrafo concluso.\n\nSecondo paragrafo.\n\nterzo in minuscolo dopo un punto."])
    assert md.count("\n\n") == 2
