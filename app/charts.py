"""Chart data extraction with verification (no extra model).

PaddleOCR-VL's 'Chart Recognition:' turns a chart into a table, but it can invent things
(on our test chart it added a Spanish 'Categoría' header and misspelled a label). Apple Vision
reads the text that is actually printed in the image. A chart table is kept only if:
  - every numeric cell appears verbatim among the numbers printed in the image,
  - every row label matches text printed in the image (Vision's spelling is used),
  - header cells not printed in the image are left empty instead of invented.
Otherwise the image stays as is, with the text Vision read as its alt text.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_NUM = re.compile(r"^[-+−]?\d[\d.,]*%?$")


def _norm_num(s: str) -> str:
    return s.replace("−", "-").replace(" ", "").rstrip("%")


def parse_chart_table(raw: str) -> list[list[str]]:
    rows = []
    for line in raw.strip().splitlines():
        if "|" not in line or re.match(r"^\s*\|?\s*:?-{2,}", line):
            continue
        rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


def _best(text: str, candidates: list[str]) -> tuple[str | None, float]:
    best, score = None, 0.0
    for c in candidates:
        r = SequenceMatcher(None, text.lower(), c.lower()).ratio()
        if r > score:
            best, score = c, r
    return best, score


def verify_chart_table(raw: str, image_lines: list[str]) -> tuple[list[list[str]] | None, str]:
    """(verified rows, reason). `image_lines` = text Apple Vision read in the chart image."""
    rows = parse_chart_table(raw)
    if len(rows) < 3 or len({len(r) for r in rows}) != 1 or len(rows[0]) < 2:
        return None, "nessuna tabella coerente"
    printed_numbers = {_norm_num(t) for line in image_lines for t in re.findall(r"[-+−]?\d[\d.,]*%?", line)}
    texts = [line.strip() for line in image_lines if re.search(r"[A-Za-zÀ-ÿ]", line)]
    header = []
    for cell in rows[0]:
        match, score = _best(cell, texts)
        header.append(match if match and score >= 0.75 else "")
    body = []
    for r in rows[1:]:
        out = []
        for cell in r:
            if _NUM.match(cell):
                if _norm_num(cell) not in printed_numbers:
                    return None, f"valore {cell} non leggibile nell'immagine"
                out.append(cell)
            elif cell:
                match, score = _best(cell, texts)
                if not match or score < 0.75:
                    return None, f"etichetta '{cell}' non presente nell'immagine"
                out.append(match)
            else:
                out.append("")
        body.append(out)
    if not any(_NUM.match(c) for r in body for c in r):
        return None, "nessun valore numerico"
    return [header, *body], "verificata"


def alt_text(image_lines: list[str], limit: int = 200) -> str:
    text = " · ".join(line.strip() for line in image_lines if line.strip())
    return (text[:limit - 1] + "…") if len(text) > limit else text


def numeric_tokens(image_lines: list[str]) -> int:
    return sum(len(re.findall(r"\d[\d.,]*", line)) for line in image_lines)
