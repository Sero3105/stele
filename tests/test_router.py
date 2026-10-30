"""Exhaustive routing matrix: every mode x page kind x engine x signal."""

import itertools

import pytest

from app import router
from app.router import ADVANCED, AUTO, DOCLING, FAST, GLM, PADDLE, VISION

KINDS = ["digital", "hybrid", "blank", "scanned"]
GOOD = dict(markdown="Testo corretto di una pagina normale con abbastanza parole.", quality=1.0, misspelled=0.0, words=60)
SHORT = dict(markdown="Capitolo 1", quality=0.3, misspelled=0.0, words=2)
EMPTY = dict(markdown="", quality=0.0, misspelled=0.0, words=0)
MISSPELLED = dict(markdown="testo " * 60, quality=1.0, misspelled=0.07, words=60)
GARBAGE = dict(markdown="(cid:1)(cid:2) �� " * 10, quality=0.1, misspelled=0.0, words=20)
MATH = dict(markdown="E = m c^2\nx = (-b ± √(b^2 - 4ac)) / 2a\na^2 + b^2 = c^2", quality=1.0, misspelled=0.0, words=12)
FORMULA_MARK = dict(markdown="Testo <!-- formula-not-decoded --> testo", quality=1.0, misspelled=0.0, words=60)


@pytest.mark.parametrize("mode,kind", itertools.product(router.MODES, KINDS))
def test_first_engine_matrix(mode, kind):
    expected = {"digital": DOCLING, "hybrid": DOCLING, "blank": DOCLING,
                "scanned": PADDLE if mode == ADVANCED else VISION}[kind]
    assert router.first_engine(kind, mode) == expected


def esc(kind, mode, engine, case, tables=0):
    r = router.escalation(kind, mode, engine, case["markdown"], case["quality"], case["misspelled"],
                          case["words"], tables)
    return r[0] if r else None


CASES = [
    # (mode, kind, engine, signals, tables, expected next engine)
    # --- GLM is the last resort in every mode
    *[(m, "scanned", GLM, c, 0, None) for m in router.MODES for c in (GOOD, MISSPELLED, GARBAGE, EMPTY)],
    # --- FAST: never a VLM; only an empty/garbled Vision page gets one Docling+OcrMac retry
    (FAST, "scanned", VISION, GOOD, 0, None),
    (FAST, "scanned", VISION, EMPTY, 0, DOCLING),
    (FAST, "scanned", VISION, GARBAGE, 0, DOCLING),
    (FAST, "scanned", VISION, SHORT, 0, None),
    (FAST, "scanned", VISION, MISSPELLED, 0, None),
    (FAST, "scanned", VISION, GOOD, 2, None),
    (FAST, "scanned", VISION, MATH, 0, None),
    (FAST, "digital", DOCLING, FORMULA_MARK, 0, None),
    (FAST, "digital", DOCLING, GARBAGE, 0, None),
    (FAST, "scanned", DOCLING, MISSPELLED, 0, None),
    # --- AUTO, Vision first pass on scanned pages
    (AUTO, "scanned", VISION, GOOD, 0, None),
    (AUTO, "scanned", VISION, SHORT, 0, None),
    (AUTO, "scanned", VISION, EMPTY, 0, PADDLE),
    (AUTO, "scanned", VISION, GOOD, 1, PADDLE),
    (AUTO, "scanned", VISION, MATH, 0, PADDLE),
    (AUTO, "scanned", VISION, MISSPELLED, 0, PADDLE),
    (AUTO, "scanned", VISION, GARBAGE, 0, PADDLE),
    # --- AUTO/ADVANCED, Docling pages
    *[(m, "digital", DOCLING, GOOD, 0, None) for m in (AUTO, ADVANCED)],
    *[(m, "digital", DOCLING, SHORT, 0, None) for m in (AUTO, ADVANCED)],
    *[(m, "digital", DOCLING, EMPTY, 0, None) for m in (AUTO, ADVANCED)],
    *[(m, "digital", DOCLING, MISSPELLED, 0, None) for m in (AUTO, ADVANCED)],  # text layer is exact
    *[(m, "digital", DOCLING, FORMULA_MARK, 0, PADDLE) for m in (AUTO, ADVANCED)],
    *[(m, "digital", DOCLING, GARBAGE, 0, PADDLE) for m in (AUTO, ADVANCED)],
    *[(m, k, DOCLING, MISSPELLED, 0, PADDLE) for m in (AUTO, ADVANCED) for k in ("hybrid", "scanned")],
    *[(m, k, DOCLING, GOOD, 0, None) for m in (AUTO, ADVANCED) for k in ("hybrid", "scanned")],
    # --- AUTO/ADVANCED, PaddleOCR-VL pages
    *[(m, "scanned", PADDLE, GOOD, 0, None) for m in (AUTO, ADVANCED)],
    *[(m, "scanned", PADDLE, SHORT, 0, None) for m in (AUTO, ADVANCED)],
    *[(m, "scanned", PADDLE, EMPTY, 0, None) for m in (AUTO, ADVANCED)],   # e.g. a photo: nothing to fix
    *[(m, "scanned", PADDLE, MISSPELLED, 0, GLM) for m in (AUTO, ADVANCED)],
    *[(m, "scanned", PADDLE, GARBAGE, 0, GLM) for m in (AUTO, ADVANCED)],
    *[(m, "scanned", PADDLE, MATH, 0, None) for m in (AUTO, ADVANCED)],
]


@pytest.mark.parametrize("mode,kind,engine,case,tables,expected", CASES)
def test_escalation_matrix(mode, kind, engine, case, tables, expected):
    assert esc(kind, mode, engine, case, tables) == expected


def test_chain_never_loops():
    """Following escalations from any start ends in <= 3 engines, without repeats."""
    for mode, kind in itertools.product(router.MODES, KINDS):
        engine, seen = router.first_engine(kind, mode), []
        while engine and engine not in seen:
            seen.append(engine)
            engine = esc(kind, mode, engine, MISSPELLED | {"markdown": "x " * 60})
        assert len(seen) <= 3 and engine is None or engine in seen


def test_better():
    old = {"quality": 1.0, "misspelled": 0.05, "words": 200}
    assert router.better(old, {"quality": 1.0, "misspelled": 0.01, "words": 190})
    assert not router.better(old, {"quality": 1.0, "misspelled": 0.0, "words": 50})       # lost content
    assert not router.better(old, {"quality": 0.8, "misspelled": 0.0, "words": 200})      # garbled
    assert not router.better(old, {"quality": 1.0, "misspelled": 0.08, "words": 200})     # more errors
    assert router.better({"quality": 0.0, "misspelled": 0.0, "words": 0},
                         {"quality": 1.0, "misspelled": 0.0, "words": 80})


def test_better_structural_escalation_accepts_latex():
    """Formulas rewritten as LaTeX have fewer dictionary words: still the wanted result."""
    old = {"quality": 1.0, "misspelled": 0.0, "words": 67}
    latex = {"quality": 1.0, "misspelled": 0.0, "words": 30}
    assert not router.better(old, latex, "5% parole non riconosciute")
    for reason in router.STRUCTURAL:
        assert router.better(old, latex, reason)
        assert not router.better(old, {"quality": 1.0, "misspelled": 0.0, "words": 0}, reason)  # empty
        assert not router.better(old, {"quality": 0.5, "misspelled": 0.0, "words": 60}, reason)  # garbled
