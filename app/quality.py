"""Engine-independent text quality checks.

OCR confidences are not comparable across engines (Vision reports ~1.0 on garbage it
is sure about; VLMs report nothing), so routing decisions use what the text itself
looks like: plausible words, junk characters, and VLM repetition loops.
"""

from __future__ import annotations

import re
import threading

# Imported here, once, on the importing thread: PyObjC resolves these lazily and a first lookup
# from several worker threads at once failed intermittently (KeyError 'NSMakeRange').
from AppKit import NSSpellChecker, NSTextCheckingTypeSpelling
from Foundation import NSMakeRange

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_JUNK = re.compile(r"[�-]|\(cid:\d+\)")
_VOWELS = set("aeiouyàèéìòùáíóúAEIOUYÀÈÉÌÒÙÁÍÓÚ")
_MD = re.compile(r"[#*_`|>\-]{1,}|\$\$?|\\[()\[\]]")


def strip_markdown(text: str) -> str:
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)  # image links are not document text
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"^\s*\|?\s*:?-{3,}.*$", " ", text, flags=re.M)  # table separator rows
    text = _MD.sub(" ", text)
    return " ".join(text.split())


_LOOP = re.compile(r"(.{8,300}?)(?:\s*\1){3,}", re.S)


def repetition_ratio(text: str) -> float:
    """Share of the text inside a fragment repeated back-to-back 4+ times (VLM decoding loops).

    Only *consecutive* repeats count: the same sentence in different paragraphs is normal prose."""
    text = " ".join(text.split())
    if len(text) < 40:
        return 0.0
    looped = sum(len(m.group(0)) for m in _LOOP.finditer(text))
    return looped / len(text)


def text_quality(text: str) -> float:
    """0 (garbage/empty) .. 1 (looks like real prose/tables)."""
    plain = strip_markdown(text)
    if len(plain) < 20:
        return 0.0 if not plain else 0.3
    junk = len(_JUNK.findall(plain)) / max(1, len(plain))
    words = _WORD.findall(plain)
    if not words:
        return 0.2  # numbers-only (e.g. a table of figures) is legitimate but unverifiable
    plausible = sum(1 for w in words if len(w) <= 25 and (len(w) <= 3 or any(c in _VOWELS for c in w)))
    alpha_share = sum(len(w) for w in words) / max(1, len(plain.replace(" ", "")))
    score = plausible / len(words)
    score *= min(1.0, 0.4 + alpha_share)  # mostly symbols -> suspicious
    score *= max(0.0, 1 - 20 * junk)
    score *= max(0.0, 1 - 1.5 * repetition_ratio(plain))
    return round(max(0.0, min(1.0, score)), 3)


_IT = {"il", "la", "di", "che", "e", "per", "con", "del", "della", "sono", "una", "nel", "gli", "è", "non"}
_EN = {"the", "of", "and", "to", "in", "is", "that", "for", "with", "are", "was", "on", "by", "this", "from"}


def guess_language(text: str) -> str | None:
    words = [w.lower() for w in _WORD.findall(text[:5000])]
    it, en = sum(w in _IT for w in words), sum(w in _EN for w in words)
    if it + en < 5:
        return None
    return "it" if it > en else "en"


_spell_lock = threading.Lock()


def misspelled_ratio(text: str, lang: str | None) -> tuple[float, int]:
    """(share of misspelled words, word count) using macOS's built-in it/en dictionaries.

    Real OCR errors make plausible-looking non-words ("refazione", "cimatizzazione"), which
    only a dictionary catches. Unknown language: the better of Italian and English.
    """
    # Math is not prose: LaTeX commands (\frac, \sqrt) would count as misspelled words.
    text = re.sub(r"\$\$.*?\$\$|\$[^$\n]+\$", " ", text, flags=re.S)
    plain = strip_markdown(text)
    if not plain:
        return 0.0, 0
    best = (1.0, 0)
    with _spell_lock:  # NSSpellChecker is a shared AppKit object: one caller at a time
        sc = NSSpellChecker.sharedSpellChecker()
        for code in ([lang] if lang else ["it", "en"]):
            sc.setLanguage_(code)
            res, _, words = sc.checkString_range_types_options_inSpellDocumentWithTag_orthography_wordCount_(
                plain, NSMakeRange(0, len(plain)), NSTextCheckingTypeSpelling, None, 0, None, None)
            ratio = len(res) / max(1, words)
            best = min(best, (ratio, words))
    return round(best[0], 4), best[1]
