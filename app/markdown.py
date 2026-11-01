"""Final document assembly and Markdown normalisation.

Engines return one Markdown fragment per page, each in its own dialect. This module turns
the ordered fragments into one coherent document. It is pure (no I/O, no engines) and
conservative: it changes layout and syntax, never wording. Nothing is invented and nothing
is dropped except page furniture (page numbers, headers/footers repeated on most pages).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace

# ------------------------------------------------------------------ blocks


@dataclass
class Block:
    kind: str   # heading | table | list | math | code | image | comment | paragraph
    text: str
    level: int = 0  # heading level
    pages: tuple[int, ...] = ()  # source pages (several when a paragraph or table spans a page break)


_LIST = re.compile(r"^(\s*)(?:[-*+•◦▪‣●–]|\d{1,3}[.)])\s+")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_IMAGE = re.compile(r"^!\[[^\]]*\]\([^)]+\)$")
_SPLIT = re.compile(r"(?<!\\)\|")


def parse(md: str) -> list[Block]:
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[Block] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        s = line.strip()
        if not s:
            i += 1
            continue
        if s.startswith("```"):
            buf = [line]
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i].rstrip())
                i += 1
            if i < len(lines):
                buf.append(lines[i].rstrip())
            i += 1
            out.append(Block("code", "\n".join(buf)))
            continue
        if s.startswith("$$"):
            buf = [s]
            closed = s != "$$" and s.endswith("$$") and len(s) > 4
            i += 1
            while not closed and i < len(lines):
                buf.append(lines[i].rstrip())
                closed = lines[i].strip().endswith("$$")
                i += 1
            out.append(Block("math", "\n".join(buf)))
            continue
        if s.startswith("<!--") and s.endswith("-->"):
            out.append(Block("comment", s))
            i += 1
            continue
        if (m := re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", s)) and m.group(2):
            out.append(Block("heading", m.group(2), len(m.group(1))))
            i += 1
            continue
        if s.startswith("|"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                buf.append(lines[i].strip())
                i += 1
            out.append(Block("table", "\n".join(buf)))
            continue
        if _IMAGE.match(s):
            out.append(Block("image", s))
            i += 1
            continue
        if _LIST.match(line):
            buf = []
            while i < len(lines) and lines[i].strip() and (_LIST.match(lines[i]) or lines[i].startswith(("  ", "\t"))):
                buf.append(lines[i].rstrip())
                i += 1
            out.append(Block("list", "\n".join(buf)))
            continue
        buf = []
        while i < len(lines):
            s2 = lines[i].strip()
            if not s2 or s2.startswith(("```", "$$", "|", "<!--")) or re.match(r"^#{1,6}\s", s2) \
                    or _IMAGE.match(s2) or (buf and _LIST.match(lines[i])):
                break
            buf.append(lines[i].rstrip())
            i += 1
        out.append(Block("paragraph", _join_lines(buf)))
    return out


def _join_lines(lines: list[str]) -> str:
    """One paragraph = one line: OCR line breaks are layout, not content. Words hyphenated at a
    line end are rejoined; explicit Markdown hard breaks (two spaces / backslash) are kept."""
    text = ""
    for line in lines:
        s = " ".join(line.split())  # runs of spaces inside a line are layout too
        if not text:
            text = s
        elif line.endswith(("  ", "\\")) or text.endswith("\\"):
            text += "\n" + s
        elif re.search(r"[A-Za-zÀ-ÿ]-$", text) and s[:1].islower():
            text = text[:-1] + s
        else:
            text += " " + s
    return text


# ------------------------------------------------------------------ per-block normalisation


def table_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        if _TABLE_SEP.match(line):
            continue
        cells = [c.strip() for c in _SPLIT.split(line.strip().strip("|"))]
        rows.append(cells)
    return rows


def render_table(rows: list[list[str]]) -> str:
    """Uniform pipe table: every row padded to the widest one (cells are never dropped)."""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _normalize_list(text: str) -> str:
    out = []
    for line in text.splitlines():
        m = re.match(r"^(\s*)([-*+•◦▪‣●–]|\d{1,3}[.)])\s+(.*)$", line)
        if not m:
            out.append(line)
            continue
        indent, marker, body = m.groups()
        marker = f"{marker[:-1]}." if marker[0].isdigit() else "-"
        out.append(f"{indent}{marker} {body.strip()}")
    return "\n".join(out)


def normalize_block(b: Block) -> Block:
    if b.kind == "table":
        rows = table_rows(b.text)
        return Block("table", render_table(rows)) if rows else b
    if b.kind == "list":
        return Block("list", _normalize_list(b.text))
    if b.kind == "heading":
        return Block("heading", re.sub(r"^\*\*(.+)\*\*$", r"\1", b.text.strip()), b.level)
    if b.kind == "math":
        body = b.text.strip()
        if body.startswith("$$") and body.endswith("$$") and "\n" not in body:
            body = "$$\n" + body[2:-2].strip() + "\n$$"
        return Block("math", body)
    return b


# ------------------------------------------------------------------ page furniture

_PAGE_LABEL = re.compile(r"^(?:pag(?:ina|e|\.)?|p\.|page)\s*\d{1,4}(?:\s*(?:di|of|/)\s*\d{1,4})?$|^[-–—]\s*\d{1,4}\s*[-–—]$"
                         r"|^\d{1,4}\s*(?:di|of|/)\s*\d{1,4}$", re.I)
_BARE = re.compile(r"^\d{1,4}$")


def _key(b: Block) -> str:
    return " ".join(re.sub(r"[#*_`]", "", b.text).lower().split())


def _edge_indexes(p: list[Block]) -> list[int]:
    return sorted({0, len(p) - 1}) if p else []


def strip_page_furniture(pages: list[list[Block]]) -> list[list[Block]]:
    """Drop page numbers and, on documents of 3+ pages, short blocks repeated at the top/bottom
    of most pages (running headers/footers), keeping their first occurrence.

    Page numbers: explicit forms ('Pagina 3', 'Page 3 of 10', '- 3 -') on a page's first/last
    block; a bare number only if it follows the page sequence (same offset from the page index
    on 3+ pages, or within 1 of it) — so a lone '2025' on a cover page stays. Running headers
    match exactly: 'Capitolo 1' and 'Capitolo 2' differ."""
    bare = [(i, k, int(b.text.strip())) for i, p in enumerate(pages) for k in _edge_indexes(p)
            if (b := p[k]).kind == "paragraph" and _BARE.match(b.text.strip())]
    offsets: dict[int, int] = {}
    for i, _, n in bare:
        offsets[n - i] = offsets.get(n - i, 0) + 1
    good = {o for o, c in offsets.items() if c >= 3} | {0, 1, 2}  # n == i+1 (+-1) always plausible
    drop = {(i, k) for i, k, n in bare if n - i in good}
    for i, p in enumerate(pages):
        for k in _edge_indexes(p):
            if p[k].kind in ("paragraph", "heading") and _PAGE_LABEL.match(p[k].text.strip()):
                drop.add((i, k))
    pages = [[b for k, b in enumerate(p) if (i, k) not in drop] for i, p in enumerate(pages)]
    if len(pages) < 3:
        return pages
    edges: dict[str, int] = {}
    for p in pages:
        for key in {_key(b) for b in p[:2] + p[-2:] if b.kind in ("paragraph", "heading") and len(b.text) <= 120}:
            edges[key] = edges.get(key, 0) + 1
    running = {k for k, n in edges.items() if k and n >= 3 and n >= 0.5 * len(pages)}
    seen: set[str] = set()
    out = []
    for p in pages:
        kept = []
        for k, b in enumerate(p):
            key = _key(b)
            at_edge = k < 2 or k >= len(p) - 2
            if key in running and at_edge:
                if key in seen:
                    continue
                seen.add(key)
            kept.append(b)
        out.append(kept)
    return out


# ------------------------------------------------------------------ page joins

_END = re.compile(r"[.!?:;…»\"”)\]]$")


def _join_boundary(prev: Block, nxt: Block) -> Block | None:
    """Merged block if `nxt` (first block of a page) continues `prev` (last block of the
    previous page), else None."""
    if prev.kind == "paragraph" and nxt.kind == "paragraph" and not _END.search(prev.text) \
            and nxt.text[:1].islower():
        if re.search(r"[A-Za-zÀ-ÿ]-$", prev.text):
            return Block("paragraph", prev.text[:-1] + nxt.text, pages=_pages(prev, nxt))
        return Block("paragraph", prev.text + " " + nxt.text, pages=_pages(prev, nxt))
    if prev.kind == "table" and nxt.kind == "table":
        a, b = table_rows(prev.text), table_rows(nxt.text)
        if a and b and len(a[0]) == len(b[0]):
            if [c.lower() for c in b[0]] == [c.lower() for c in a[0]]:
                b = b[1:]  # header repeated on the continuation page
            return Block("table", render_table(a + b), pages=_pages(prev, nxt))
    return None


def _pages(*blocks: Block) -> tuple[int, ...]:
    return tuple(sorted({p for b in blocks for p in b.pages}))


# ------------------------------------------------------------------ document


def fix_heading_levels(blocks: list[Block]) -> list[Block]:
    """No skipped levels (# then ### becomes # then ##); an H1 identical to the first H1
    (title repeated on several pages) is dropped. Distinct headings are never removed."""
    out, prev, first_h1 = [], 0, None
    for b in blocks:
        if b.kind != "heading":
            out.append(b)
            continue
        level = b.level
        if level == 1:
            if first_h1 is None:
                first_h1 = _key(b)
            elif _key(b) == first_h1:
                continue
        if prev and level > prev + 1:
            level = prev + 1
        out.append(replace(b, level=level))
        prev = level
    return out


def _text(b: Block) -> str:
    return ("#" * b.level + " " + b.text) if b.kind == "heading" else b.text


def page_marker(n: int) -> str:
    return f"<!-- pagina {n} -->"


def render(blocks: list[Block], markers: bool = False) -> str:
    """`markers`: a `<!-- pagina N -->` line where page N starts (invisible when rendered)."""
    parts, page = [], 0
    for b in blocks:
        if markers and b.pages and b.pages[0] > page:
            page = b.pages[0]
            parts.append(page_marker(page))
        parts.append(_text(b))
    return "\n\n".join(parts)


def front_matter(meta: dict) -> str:
    """YAML front matter (Obsidian 'properties'), only keys that have a value. Text is quoted,
    numbers stay numbers, lists become YAML lists (e.g. tags)."""
    def q(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip() + '"'

    def value(v) -> str:
        if isinstance(v, bool) or not isinstance(v, (int, list)):
            return q(str(v))
        return str(v) if isinstance(v, int) else "[" + ", ".join(q(str(x)) for x in v) + "]"
    items = [(k, value(v)) for k, v in meta.items() if v not in (None, "", []) and str(v).strip()]
    if not items:
        return ""
    return "---\n" + "\n".join(f"{k}: {v}" for k, v in items) + "\n---\n\n"


def merge_continuations(blocks: list[Block]) -> list[Block]:
    """Rejoin what an engine split: a paragraph cut mid-sentence (OCR returned visual lines as
    paragraphs) and list items separated by blank lines (one list, not one list per item)."""
    out: list[Block] = []
    for b in blocks:
        if out and b.kind == out[-1].kind == "list":
            out[-1] = Block("list", out[-1].text + "\n" + b.text, pages=_pages(out[-1], b))
            continue
        if out and (merged := _join_boundary(out[-1], b)) and b.kind == "paragraph":
            out[-1] = merged
            continue
        out.append(b)
    return out


def assemble(pages: list[str], meta: dict[str, str] | None = None) -> str:
    """Ordered page fragments -> one normalised Markdown document."""
    return document(assemble_blocks(pages), meta)


def document(blocks: list[Block], meta: dict[str, str] | None = None, markers: bool = False) -> str:
    body = render(blocks, markers).strip()
    return front_matter(meta or {}) + (body + "\n" if body else "")


def assemble_blocks(pages: list[str], numbers: list[int] | None = None) -> list[Block]:
    """Ordered page fragments -> normalised blocks, each knowing its source page(s) (`numbers`,
    one per fragment; 0 = a format without pages)."""
    numbers = numbers or [0] * len(pages)
    page_blocks = [[replace(normalize_block(b), pages=(n,) if n else ()) for b in parse(p)]
                   for p, n in zip(pages, numbers)]
    page_blocks = strip_page_furniture(page_blocks)
    blocks: list[Block] = []
    for p in page_blocks:
        if blocks and p:
            merged = _join_boundary(blocks[-1], p[0])
            if merged:
                blocks[-1] = merged
                p = p[1:]
        blocks.extend(p)
    blocks = merge_continuations(blocks)
    return fix_heading_levels(blocks)


# ------------------------------------------------------------------ chapters (one note each)


def split_chapters(blocks: list[Block]) -> tuple[list[Block], list[tuple[str, list[Block]]]]:
    """(blocks before the first chapter, [(chapter title, its blocks)]). A chapter starts at each
    heading of the highest level that occurs at least twice (a lone H1 is the document title);
    headings are promoted so that each chapter's own heading becomes '#'. No such level: no
    chapters."""
    levels = [b.level for b in blocks if b.kind == "heading"]
    level = next((lv for lv in sorted(set(levels)) if levels.count(lv) >= 2), None)
    if level is None:
        return blocks, []
    intro: list[Block] = []
    chapters: list[tuple[str, list[Block]]] = []
    for b in blocks:
        if b.kind == "heading" and b.level == level:
            chapters.append((b.text, []))
        if b.kind == "heading" and chapters:
            b = replace(b, level=max(1, b.level - level + 1))
        (chapters[-1][1] if chapters else intro).append(b)
    while chapters and all(b.kind == "heading" for b in chapters[0][1]):  # a title right before the first chapter
        intro += [replace(b, level=1) for b in chapters.pop(0)[1]]
    return intro, chapters if len(chapters) >= 2 else []


def page_span(blocks: list[Block]) -> str:
    pages = sorted({p for b in blocks for p in b.pages})
    if not pages:
        return ""
    return str(pages[0]) if pages[0] == pages[-1] else f"{pages[0]}-{pages[-1]}"


# ------------------------------------------------------------------ chunks (RAG)

MAX_CHUNK_CHARS = 2000  # a long section is split at block boundaries (a single bigger block stays whole)


def chunks(blocks: list[Block], engines: dict[int, str] | None = None) -> list[dict]:
    """Sections for retrieval: every heading starts a chunk that carries its heading path, the
    source pages and the engines that read them. Pure: image links are rewritten by the caller."""
    out: list[dict] = []
    path: list[tuple[int, str]] = []
    cur: list[Block] = []

    def flush() -> None:
        if not any(b.kind != "heading" for b in cur):
            return  # a heading alone is not content: it lives on in the next chunk's heading path
        pages = sorted({p for b in cur for p in b.pages})
        used = engines or {}
        out.append({"headings": [t for _, t in path], "text": "\n\n".join(_text(b) for b in cur),
                    "pages": pages, "engines": sorted({used[p] for p in pages or used if p in used})})

    for b in blocks:
        if b.kind == "heading":
            flush()
            cur = []
            path = [h for h in path if h[0] < b.level] + [(b.level, b.text)]
        elif cur and sum(len(x.text) for x in cur) + len(b.text) > MAX_CHUNK_CHARS:
            flush()
            cur = []
        cur.append(b)
    flush()
    return out


_MARKER = re.compile(r"^<!-- pagina (\d+) -->$")


def blocks_from_markdown(md: str) -> list[Block]:
    """Blocks of a published (possibly hand-edited) document, their pages taken from its
    `<!-- pagina N -->` markers."""
    md = re.sub(r"\A---\n.*?\n---\n", "", md, flags=re.S)
    out, page = [], 0
    for b in parse(md):
        if b.kind == "comment" and (m := _MARKER.match(b.text)):
            page = int(m.group(1))
            continue
        out.append(replace(b, pages=(page,) if page else ()))
    return out


def jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


# ------------------------------------------------------------------ images

_IMG_REF = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")


def image_refs(md: str) -> list[str]:
    return [m.group(2) for m in _IMG_REF.finditer(md)]


def rewrite_images(md: str, mapping: dict[str, str]) -> str:
    """Point image links at their final names; a link whose file doesn't exist (mapped to None)
    is replaced by its alt text, so no broken link and no lost caption."""
    def sub(m: re.Match) -> str:
        new = mapping.get(m.group(2), m.group(2))
        return f"![{m.group(1)}]({new})" if new else m.group(1)
    return _IMG_REF.sub(sub, md)
