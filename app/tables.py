"""Keep table structure when GLM-OCR (page mode, plain text) replaces PaddleOCR-VL on a page.

On degraded scans PaddleOCR-VL gets the table *structure* right but misreads cells, while
GLM-OCR reads the text better but returns table rows as plain lines. `restore_tables`
combines them: Paddle's columns/header shape + GLM's values. It never invents cells and never
drops a number: when a safe reconstruction isn't possible the GLM lines stay as they are.
"""

from __future__ import annotations

import re

from app.markdown import Block, parse, render, render_table, table_rows

_NUM = re.compile(r"^[-+−(]?\d[\d.,'’]*%?\)?$|^[-–—]$")


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d[\d.,]*\d|\d", text))


def _split_row(text: str, cols: int) -> list[str] | None:
    """'Scuola Rossi 2450 182300 74,4' -> ['Scuola Rossi', '2450', '182300', '74,4'] when the last
    cols-1 tokens are numbers; None when the line can't be split safely."""
    tokens = text.split()
    if len(tokens) < cols or not all(_NUM.match(t) for t in tokens[len(tokens) - cols + 1:]):
        return None
    head = tokens[:len(tokens) - cols + 1]
    if cols > 1 and any(_NUM.match(t) for t in head[1:]):
        return None  # ambiguous: numbers inside the label
    return [" ".join(head), *tokens[len(tokens) - cols + 1:]]


def _split_header(text: str, model: list[str]) -> list[str] | None:
    """Split a plain header line the way the layout model's header is split (same word counts)."""
    tokens, counts = text.split(), [len(c.split()) for c in model]
    if not all(counts) or sum(counts) != len(tokens):
        return None
    out, i = [], 0
    for c in counts:
        out.append(" ".join(tokens[i:i + c]))
        i += c
    return out


def _good(rows: list[list[str]]) -> bool:
    cells = [c for r in rows for c in r]
    return len(rows) >= 2 and sum(1 for c in cells if c) >= 0.8 * len(cells)


def restore_tables(md: str, layout_blocks: list[dict]) -> tuple[str, list[str]]:
    """Return (markdown, notes). `layout_blocks` are PaddleOCR-VL blocks of the same page."""
    tables = [table_rows(b["text"]) for b in layout_blocks if b.get("task") == "table" and b.get("text")]
    tables = [t for t in tables if t and len(t[0]) >= 2]
    blocks = parse(md)
    notes: list[str] = []
    for rows in tables:
        cols = len(rows[0])
        if any(b.kind == "table" and len(table_rows(b.text)[0]) == cols for b in blocks):
            notes.append("tabella già strutturata nel testo")
            continue
        # Longest run of consecutive paragraphs that split into `cols` cells.
        best: tuple[int, int] = (0, 0)
        i = 0
        while i < len(blocks):
            j = i
            while j < len(blocks) and blocks[j].kind == "paragraph" and _split_row(blocks[j].text, cols):
                j += 1
            if j - i > best[1] - best[0]:
                best = (i, j)
            i = j + 1 if j == i else j
        start, end = best
        if end - start >= max(1, (len(rows) - 1) // 2):
            data = [_split_row(blocks[k].text, cols) for k in range(start, end)]
            header = None
            if start > 0 and blocks[start - 1].kind == "paragraph":
                header = _split_header(blocks[start - 1].text, rows[0])
                if header:
                    start -= 1
            if header is None:
                header = rows[0]  # layout model's header; no header words of GLM are dropped below
            blocks[start:end] = [Block("table", render_table([header] + data))]
            notes.append(f"tabella ricostruita ({len(data)} righe) da struttura layout + testo GLM")
            continue
        # Not reconstructible from GLM: use the layout table only if it is complete and every number
        # in it is confirmed by the GLM text; replace the GLM lines that hold the table's content.
        table_text = " ".join(c for r in rows for c in r)
        if _good(rows) and _numbers(table_text) <= _numbers(md):
            cells = {c.lower() for r in rows for c in r if c}
            hits = [k for k, b in enumerate(blocks) if b.kind == "paragraph"
                    and sum(1 for t in b.text.split() if t.lower() in cells or any(t.lower() in c for c in cells))
                    >= 0.6 * len(b.text.split())]
            covered = " ".join(blocks[k].text for k in hits)
            if hits and _numbers(covered) <= _numbers(table_text):
                blocks[hits[0]:hits[-1] + 1] = [Block("table", render_table(rows))] + [
                    b for k, b in enumerate(blocks[hits[0]:hits[-1] + 1], hits[0]) if k not in hits]
                notes.append("tabella del layout inserita (valori confermati dal testo)")
                continue
        notes.append("tabella non ricostruibile con sicurezza: righe lasciate come testo")
    return render(blocks), notes
