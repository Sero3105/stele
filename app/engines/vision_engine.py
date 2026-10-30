"""Apple Vision engine: native macOS OCR + document structure through the Swift helper.

The helper (native/vision_ocr.swift) runs RecognizeTextRequest (lines + confidence) and
RecognizeDocumentsRequest (paragraphs, tables, lists in reading order). It ships with
the OS: no weights to download, runs on the Neural Engine.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from app import config
from app.engines.base import Engine, PageOutput
from app.resources import EngineResources

log = logging.getLogger(__name__)


def _inside(inner: dict, outer: dict, tol: float = 0.01) -> bool:
    cx, cy = inner["x"] + inner["w"] / 2, inner["y"] + inner["h"] / 2
    return (outer["x"] - tol <= cx <= outer["x"] + outer["w"] + tol
            and outer["y"] - tol <= cy <= outer["y"] + outer["h"] + tol)


def md_table(rows: list[list[str]]) -> str:
    rows = [[c.replace("|", "\\|").replace("\n", " ").strip() for c in r] for r in rows if r]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def blocks_to_markdown(result: dict) -> str:
    """Rebuild a page from Vision's blocks: paragraphs keep Vision's reading order, each table/list
    is emitted where its first contained paragraph would have been and its cells are not repeated."""
    blocks = result.get("blocks") or []
    containers = [b for b in blocks if b["kind"] in ("table", "list")]
    paragraphs = [b for b in blocks if b["kind"] == "paragraph"]
    if not paragraphs and not containers:
        return "\n".join(line["text"] for line in result.get("lines", []))
    heading = _heading_levels(result, paragraphs)
    placed: set[int] = set()
    parts: list[str] = []
    for k, p in enumerate(paragraphs):
        owner = next((i for i, c in enumerate(containers) if _inside(p["box"], c["box"])), None)
        if owner is None:
            parts.append(("#" * heading[k] + " " if heading.get(k) else "") + p["text"].strip())
        elif owner not in placed:
            placed.add(owner)
            parts.append(_render(containers[owner]))
    # Containers with no paragraph inside them (rare): append in vertical order.
    for i, c in sorted(enumerate(containers), key=lambda ic: ic[1]["box"]["y"]):
        if i not in placed:
            parts.append(_render(c))
    return "\n\n".join(p for p in parts if p)


def _heading_levels(result: dict, paragraphs: list[dict]) -> dict[int, int]:
    """Vision has no heading labels. A short one-line paragraph whose characters are clearly wider
    than the body text's (bigger font) is a heading.
    Character width = line width / characters, from the line-level recognition."""
    widths = {}
    for line in result.get("lines", []):
        text = line["text"].strip()
        if len(text) >= 3:
            widths.setdefault(text, line["box"]["w"] / len(text))
    body_lines = sorted(w for t, w in widths.items() if len(t) > 25)
    if len(body_lines) < 3:
        return {}
    body = body_lines[len(body_lines) // 2]
    big = {k: widths[p["text"].strip()] for k, p in enumerate(paragraphs)
           if p["text"].strip() in widths and len(p["text"]) < 120
           and widths[p["text"].strip()] >= 1.2 * body}
    if not big:
        return {}
    # Level from the (tight) paragraph box height: '#' only for a heading clearly taller than the
    # other headings on the page; a page with one heading size gets '##' throughout.
    heights = {k: paragraphs[k]["box"]["h"] for k in big}
    tallest = max(heights.values())
    mixed = any(h < 0.85 * tallest for h in heights.values())
    return {k: 1 if mixed and h >= 0.85 * tallest else 2 for k, h in heights.items()}


def _render(c: dict) -> str:
    if c["kind"] == "table":
        return md_table(c.get("rows") or [])
    return "\n".join(f"- {item.strip().lstrip('•-–* ').strip()}" for item in c.get("items") or [])


def mean_confidence(result: dict) -> float | None:
    lines = result.get("lines") or []
    total = sum(len(line["text"]) for line in lines)
    return sum(line["confidence"] * len(line["text"]) for line in lines) / total if total else None


class VisionEngine(Engine):
    name = "apple-vision"
    # Measured: helper process peak ~120 MB per page batch (benchmarks/REPORT.md).
    resources = EngineResources(model_memory_mb=0, estimated_working_memory_mb=300, max_parallel_jobs=3)
    child_peak = 0  # largest helper-process peak footprint seen (bytes), for the stress benchmark

    def available(self) -> tuple[bool, str]:
        if not config.VISION_HELPER.exists():
            return False, "native/vision_ocr not built (run `make -C native`)"
        return True, ""

    def recognize(self, images: list[Path], langs: str = "it-IT,en-US") -> list[dict]:
        def call(_):
            cmd = [str(config.VISION_HELPER), "--langs", langs, *map(str, images)]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60 + 30 * len(images))
            if proc.returncode != 0:
                raise RuntimeError(f"vision helper failed: {proc.stderr.strip()[:500]}")
            # split("\n") only: splitlines() also breaks on U+2028/U+2029, which the
            # Swift JSONEncoder emits raw inside OCR text -> truncated JSON lines.
            rows = [json.loads(line) for line in proc.stdout.split("\n") if line.strip()]
            for r in rows:
                if "peak_footprint" in r:
                    self.child_peak = max(self.child_peak, r["peak_footprint"])
            return [r for r in rows if "peak_footprint" not in r]
        return self.run(call)

    def convert_images(self, images: list[Path], page_numbers: list[int]) -> list[PageOutput]:
        out = []
        for res, page_no in zip(self.recognize(images), page_numbers):
            if res.get("error"):
                raise RuntimeError(f"Apple Vision: {res['error']}")
            out.append(PageOutput(page_no, blocks_to_markdown(res), self.name,
                                  data={"ocr_confidence": mean_confidence(res), "blocks": res.get("blocks", [])}))
        return out
