"""What Docling's Office parsers leave out: PPTX speaker notes, XLSX formulas and merged cells."""

from __future__ import annotations

from pathlib import Path


def pptx_notes(path: Path) -> dict[int, str]:
    """1-based slide number -> speaker notes text."""
    from pptx import Presentation
    notes = {}
    for i, slide in enumerate(Presentation(str(path)).slides, start=1):
        if slide.has_notes_slide:
            text = slide.notes_slide.notes_text_frame.text.strip()
            if text:
                notes[i] = text
    return notes


def xlsx_details(path: Path) -> list[dict]:
    """Per sheet (workbook order, chart sheets included, like Docling's pages): name, used range,
    merged ranges, formulas with cached values."""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=False, read_only=False)
    sheets = []
    for name in wb.sheetnames:
        ws = wb[name]
        if not hasattr(ws, "iter_rows"):  # chart sheet: no cells
            sheets.append({"name": name, "dimensions": "", "state": ws.sheet_state, "merged": [],
                           "formulas": [], "charts": len(getattr(ws, "_charts", []))})
            continue
        formulas = [{"cell": c.coordinate, "formula": c.value, "value": None}
                    for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
        sheets.append({"name": name, "dimensions": ws.dimensions, "state": ws.sheet_state,
                       "merged": [str(r) for r in ws.merged_cells.ranges], "formulas": formulas,
                       "charts": len(getattr(ws, "_charts", []))})
    if any(s["formulas"] for s in sheets):  # second full load only when there are values to read
        values_wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
        for s in sheets:
            for f in s["formulas"]:
                f["value"] = values_wb[s["name"]][f["cell"]].value
    return sheets
