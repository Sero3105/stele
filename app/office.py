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
    """Per sheet (workbook order): name, used range, merged ranges, formulas with cached values."""
    import openpyxl
    formulas_wb = openpyxl.load_workbook(path, data_only=False, read_only=False)
    values_wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    sheets = []
    for ws in formulas_wb.worksheets:
        vs = values_wb[ws.title]
        formulas = []
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    formulas.append({"cell": cell.coordinate, "formula": cell.value,
                                     "value": vs[cell.coordinate].value})
        sheets.append({"name": ws.title, "dimensions": ws.dimensions, "state": ws.sheet_state,
                       "merged": [str(r) for r in ws.merged_cells.ranges], "formulas": formulas,
                       "charts": len(getattr(ws, "_charts", []))})
    return sheets
