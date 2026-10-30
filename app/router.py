"""Intelligent routing: which engine converts each page, and when to escalate.

Pure functions (no I/O) so the policy is unit-tested on its own. Thresholds come from
benchmarks/REPORT.md, not from engine confidences: Docling's ocr_score was 1.0 on a
page whose real CER was 20%.

Chain for scanned pages (auto):  Apple Vision  ->  PaddleOCR-VL (layout)  ->  GLM-OCR
  - Vision (~0.4 s/page, no weights) handles simple text pages.
  - Pages Vision shows to be complex (tables, formulas) or that it read badly go to PaddleOCR-VL.
  - Pages still bad after PaddleOCR-VL (heavily degraded scans) get GLM-OCR as last resort.
Digital pages never get OCR: Docling reads the exact text layer.
"""

from __future__ import annotations

import re

FAST, AUTO, ADVANCED = "fast", "auto", "advanced"
MODES = (AUTO, FAST, ADVANCED)

DOCLING = "docling"   # text layer + layout + TableFormer (+ OcrMac on bitmap regions)
VISION = "vision"     # Apple Vision helper
PADDLE = "paddle"     # PaddleOCR-VL 1.6, PP-DocLayoutV3 + per-block recognition (MLX)
GLM = "glm"           # GLM-OCR (MLX), fallback for pages the others read badly

# Calibrated on benchmark outputs (REPORT.md): good pages have <=0.8% misspelled words,
# poor OCR 2.7-13%. text_quality catches junk characters and VLM repetition loops.
MAX_MISSPELLED = 0.02
MIN_WORDS_FOR_SPELLING = 30
MIN_WORDS_TO_JUDGE = 5
MIN_QUALITY = 0.6
FORMULA_MARK = "formula-not-decoded"
# Escalations made for structure (not for misread text): the stronger engine's output is wanted
# even if a word count drops, e.g. formulas rewritten as LaTeX.
TABLES, FORMULAS, FORMULAS_UNDECODED = "tabelle: serve la struttura", "formule", "formule non decodificate"
STRUCTURAL = {TABLES, FORMULAS, FORMULAS_UNDECODED}
_MATH = re.compile(r"[=^√∑∫±∞≤≥≠∂∆πλμσ]|\b\d+\s*/\s*\d+\b")


def first_engine(page_kind: str, mode: str) -> str:
    """Engine for the first pass over a page."""
    if page_kind in ("digital", "hybrid", "blank"):
        return DOCLING  # the text layer is exact; OCR could only make it worse
    return PADDLE if mode == ADVANCED else VISION


def looks_mathematical(md: str) -> bool:
    lines = [l for l in md.splitlines() if l.strip()]
    return bool(lines) and sum(1 for l in lines if len(_MATH.findall(l)) >= 2) / len(lines) > 0.15


def bad_text(quality: float, misspelled: float, words: int) -> str | None:
    """Why a page's text looks wrong, or None. Under MIN_WORDS_TO_JUDGE words (a cover page,
    a caption) there is too little text to judge: such pages are never escalated for quality."""
    if words < MIN_WORDS_TO_JUDGE:
        return None
    if quality < MIN_QUALITY:
        return f"qualità testo bassa ({quality:.2f})"
    if words >= MIN_WORDS_FOR_SPELLING and misspelled > MAX_MISSPELLED:
        return f"{misspelled:.0%} parole non riconosciute"
    return None


def escalation(page_kind: str, mode: str, engine: str, markdown: str, quality: float,
               misspelled: float = 0.0, words: int = 0, tables: int = 0) -> tuple[str, str] | None:
    """(next engine, reason) if this page should be redone by a stronger engine, else None.

    `tables` = tables the first engine detected (Vision reports them even when it reads cells badly).
    Blank pages never get here (the pipeline filters them by ink coverage before any OCR).
    """
    empty = not markdown.strip()
    if engine == GLM:
        return None
    if mode == FAST:
        # Fast never loads a VLM; a Vision page that came back empty/garbled falls back to Docling + OcrMac.
        if engine == VISION and (empty or bad_text(quality, 0.0, words)):
            return DOCLING, "testo OCR scarso"
        return None
    if engine == PADDLE:
        reason = bad_text(quality, misspelled, words)  # empty Paddle output (e.g. a photo page) stays
        return (GLM, reason) if reason else None
    if engine == DOCLING:
        if FORMULA_MARK in markdown:
            return PADDLE, FORMULAS_UNDECODED
        if page_kind == "digital":
            return (PADDLE, "strato di testo PDF corrotto") if bad_text(quality, 0.0, words) else None
        reason = bad_text(quality, misspelled, words)  # scanned/hybrid pages OCRed by Docling + OcrMac
        return (PADDLE, reason) if reason else None
    # Vision (first pass on scanned pages in AUTO)
    if empty:
        return PADDLE, "nessun testo riconosciuto"
    if tables:
        return PADDLE, TABLES
    if looks_mathematical(markdown):
        return PADDLE, FORMULAS
    reason = bad_text(quality, misspelled, words)
    return (PADDLE, reason) if reason else None


def better(old: dict, new: dict, reason: str = "") -> bool:
    """Keep an escalated result unless it is clearly worse or lost content (dicts: quality,
    misspelled, words). Structural escalations only require non-empty, non-garbled output."""
    if reason in STRUCTURAL:
        return new["words"] > 0 and new["quality"] >= old["quality"] - 0.1
    if new["words"] < 0.6 * old["words"]:
        return False
    if new["quality"] < old["quality"] - 0.1:
        return False
    return new["misspelled"] <= old["misspelled"] + 0.01
