"""Advanced OCR with document VLMs on MLX (Metal): GLM-OCR and PaddleOCR-VL.

Two modes, both measured in benchmarks/REPORT.md:
  page   — the whole page image goes to the VLM with a "document to Markdown" prompt.
  layout — PP-DocLayoutV3 (MLX) finds blocks + reading order, each block is cropped and
           recognised with a task prompt (text / table / formula). This is the official
           PaddleOCR-VL pipeline shape and also what GLM-OCR's own SDK does.
"""

from __future__ import annotations

import gc
import html
import logging
import re
import threading
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image

from app import config
from app.engines.base import Engine, PageOutput
from app.engines.vision_engine import md_table
from app.resources import EngineResources

log = logging.getLogger(__name__)

PROMPTS = {
    "glm": {"text": "Text Recognition:", "table": "Table Recognition:", "formula": "Formula Recognition:"},
    "paddle": {"text": "OCR:", "table": "Table Recognition:", "formula": "Formula Recognition:",
               "chart": "Chart Recognition:"},
}
# One MLX generation at a time across all VLM engines: MLX doesn't promise thread-safe concurrent
# evaluation, and both models share the same GPU anyway.
_MLX_LOCK = threading.Lock()

# PP-DocLayoutV3 labels -> how we treat the crop.
SKIP = {"header", "footer", "number", "header_image", "footer_image", "seal"}
PICTURE = {"image", "chart"}
TABLE = {"table"}
FORMULA = {"formula", "display_formula", "inline_formula"}
TITLE = {"doc_title"}
HEADING = {"paragraph_title"}


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            self._cell = []
            span = int(dict(attrs).get("colspan") or 1)
            self._span = span

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            if not self.rows:
                self.rows.append([])
            text = " ".join("".join(self._cell).split())
            self.rows[-1] += [text] + [""] * (self._span - 1)
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def html_tables_to_md(text: str) -> str:
    def conv(m: re.Match) -> str:
        p = _TableParser()
        p.feed(m.group(0))
        return "\n\n" + md_table(p.rows) + "\n\n"
    return re.sub(r"<table\b.*?</table>", conv, text, flags=re.S | re.I)


def otsl_to_md(text: str) -> str:
    """PaddleOCR-VL table tokens: <fcel> cell, <ecel> empty, <lcel>/<ucel>/<xcel> spans, <nl> row end."""
    if "<fcel>" not in text and "<ecel>" not in text:
        return text
    rows: list[list[str]] = []
    for raw_row in re.split(r"<nl>", text):
        cells = re.findall(r"<(fcel|ecel|lcel|ucel|xcel)>([^<]*)", raw_row)
        if cells:
            rows.append([c.strip() if t == "fcel" else "" for t, c in cells])
    return md_table(rows)


_SUP = str.maketrans("0123456789+-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻")


def _plain_math(m: re.Match) -> str:
    """`$Superficie (m^{2})$` is prose the model wrapped in math: give it back as text."""
    body = m.group(1)
    is_unit = len(body) <= 12 and "\\" not in body and re.fullmatch(r"[A-Za-z ]*\^\{?[0-9+-]+\}?", body.strip())
    if not is_unit and (not re.search(r"[A-Za-zÀ-ÿ]{3,}\s", body) or re.search(r"\\(frac|sum|int|sqrt|begin)", body)):
        return m.group(0)
    body = re.sub(r"\^\{?([0-9+-]+)\}?", lambda s: s.group(1).translate(_SUP), body)
    return re.sub(r"\\(mathrm|text)\{([^}]*)\}", r"\2", body)


def clean_output(text: str) -> str:
    text = re.sub(r"<\|[^>]*\|>|</?(s|im_start|im_end)>", "", text)
    text = re.sub(r"\\\((.+?)\\\)", lambda m: "$" + m.group(1).replace("{{", "{").replace("}}", "}") + "$", text)
    text = re.sub(r"(?<!\$)\$([^$\n]+)\$(?!\$)", _plain_math, text)
    text = otsl_to_md(text)
    text = html_tables_to_md(text)
    if "<" in text and re.search(r"</?(p|br|div|span|b|i)\b", text):
        text = re.sub(r"<br\s*/?>", "\n", text)
        text = re.sub(r"</?(p|div|span|b|i)\b[^>]*>", "", text)
    return html.unescape(text).strip()


class VlmEngine(Engine):
    def __init__(self, key: str, family: str, mode: str, resources: EngineResources, max_tokens: int = 4096,
                 max_side: int | None = None):
        super().__init__()
        self.key, self.family, self.mode = key, family, mode
        self.max_side = max_side  # page mode: downscale so fewer visual tokens are prefilled
        self.name = f"{key}:{mode}"
        self.resources = resources
        self.max_tokens = max_tokens

    def available(self) -> tuple[bool, str]:
        from app.models import local_path
        missing = [k for k in [self.key] + (["pp-doclayout-v3"] if self.mode == "layout" else [])
                   if local_path(k) is None]
        return (False, f"models not installed: {missing} (python -m app.models download)") if missing else (True, "")

    def _load(self):
        import mlx.core as mx
        from mlx_vlm import load
        from mlx_vlm.utils import load_model

        from app.models import local_path
        # Bound MLX's buffer cache so freed activations go back to the OS instead of piling up.
        mx.set_cache_limit(512 * config.MB)
        model, processor = load(str(local_path(self.key)))
        layout = None
        if self.mode == "layout":
            from mlx_vlm.models.indic_ocr.pipeline import MLXLayoutBackend
            lm = load_model(local_path("pp-doclayout-v3"))
            lm.eval()
            layout = MLXLayoutBackend(lm)
        log.info("loaded %s (%s mode)", self.key, self.mode)
        return model, processor, layout

    def _unload(self, obj) -> None:
        import mlx.core as mx
        del obj
        gc.collect()
        mx.clear_cache()

    def _generate(self, model, processor, image: Image.Image, prompt: str, max_tokens: int) -> str:
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template
        chat = apply_chat_template(processor, model.config, prompt, num_images=1)
        out = generate(model, processor, chat, [image], max_tokens=max_tokens, temperature=0.0, verbose=False)
        return out.text if hasattr(out, "text") else str(out)

    def _generate_many(self, model, processor, items: list[tuple[Image.Image, str, int]]) -> list[str]:
        # Sequential on purpose: mlx-vlm batch_generate gave no speed-up for GLM-OCR and worse
        # text, and fails for PaddleOCR-VL (benchmarks/REPORT.md, "Batch inference").
        return [self._generate(model, processor, im, pr, mt) for im, pr, mt in items]

    def pages_markdown(self, images: list[Image.Image], assets: Path | None = None,
                       prefixes: list[str] | None = None) -> list[tuple[str, dict]]:
        """Markdown for several pages under one memory lease (model stays loaded across them)."""
        prefixes = prefixes or [f"p{i + 1}" for i in range(len(images))]

        def work(obj):
            with _MLX_LOCK:
                return _work(obj)

        def _work(obj):
            model, processor, layout = obj
            prompts = PROMPTS[self.family]
            if self.mode == "page":
                pages = [_fit(im, self.max_side) for im in images]
                texts = self._generate_many(model, processor, [(im, prompts["text"], self.max_tokens) for im in pages])
                return [(_paragraphs(clean_output(t)), {}) for t in texts]
            pages: list[list[dict]] = []
            items: list[tuple[Image.Image, str, int]] = []
            owners: list[dict] = []
            for im, prefix in zip(images, prefixes):
                recs = []
                for k, b in enumerate(layout.detect(im)):
                    x0, y0, x1, y1 = b.bbox_xyxy
                    rec = {"label": b.label, "bbox": [round(v) for v in b.bbox_xyxy], "conf": b.conf}
                    recs.append(rec)
                    if b.label in SKIP or x1 - x0 < 4 or y1 - y0 < 4:
                        continue
                    crop = im.crop((int(x0), int(y0), int(x1), int(y1)))
                    if b.label in PICTURE:
                        if assets is not None:
                            assets.mkdir(parents=True, exist_ok=True)
                            name = f"{prefix}_{b.label}{k}.png"
                            crop.save(assets / name)
                            rec["image"] = f"{assets.name}/{name}"
                        continue
                    task = "table" if b.label in TABLE else "formula" if b.label in FORMULA else "text"
                    rec["task"] = task
                    items.append((crop, prompts[task], self.max_tokens if task == "table" else 1024))
                    owners.append(rec)
                pages.append(recs)
            for rec, text in zip(owners, self._generate_many(model, processor, items)):
                rec["text"] = clean_output(text)
            return [(_blocks_markdown(recs), {"blocks": recs}) for recs in pages]
        return self.run(work)

    def chart_tables(self, images: list[Image.Image]) -> list[str | None]:
        """For each image: None if the layout model finds no chart in it, else the model's raw
        chart-to-table output for the largest chart (to be verified by app.charts)."""
        def work(obj):
            with _MLX_LOCK:
                model, processor, layout = obj
                out: list[str | None] = []
                for im in images:
                    charts = [b for b in layout.detect(im) if b.label == "chart"]
                    if not charts:
                        out.append(None)
                        continue
                    x0, y0, x1, y1 = max(charts, key=lambda b: (b.bbox_xyxy[2] - b.bbox_xyxy[0])
                                         * (b.bbox_xyxy[3] - b.bbox_xyxy[1])).bbox_xyxy
                    crop = im.crop((int(x0), int(y0), int(x1), int(y1)))
                    out.append(self._generate(model, processor, crop, PROMPTS[self.family]["chart"], 1024))
                return out
        return self.run(work)

    def page_markdown(self, image: Image.Image, assets: Path | None = None, prefix: str = "p1") -> tuple[str, dict]:
        return self.pages_markdown([image], assets, [prefix])[0]

    def convert_images(self, images: list[Path], page_numbers: list[int], assets: Path | None = None) -> list[PageOutput]:
        pil = []
        for path in images:
            with Image.open(path) as im:
                pil.append(im.convert("RGB"))
        res = self.pages_markdown(pil, assets, [f"p{n}" for n in page_numbers])
        return [PageOutput(n, md, self.name, data=data) for n, (md, data) in zip(page_numbers, res)]


def _paragraphs(text: str) -> str:
    """Page-mode output puts one paragraph per line; Markdown needs blank lines between them
    (table rows must stay contiguous)."""
    out: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if out and not (line.lstrip().startswith("|") and out[-1].lstrip().startswith("|")):
            out.append("")
        out.append(line)
    return "\n".join(out)


def transfer_headings(md: str, blocks: list[dict]) -> str:
    """Mark as headings the lines of a plain-text page that match title blocks found by the
    layout model in an earlier pass (GLM-OCR page mode returns text without structure)."""
    from difflib import SequenceMatcher
    heads = [(("# " if b["label"] in TITLE else "## "), b.get("text", "").lstrip("# ").strip())
             for b in blocks if b.get("label") in TITLE | HEADING and b.get("text")]
    if not heads:
        return md
    lines = md.splitlines()
    for prefix, head in heads:
        best, score = None, 0.75
        for i, line in enumerate(lines):
            if line.startswith("#") or not line.strip() or len(line) > 2 * len(head) + 10:
                continue
            r = SequenceMatcher(None, line.strip().lower(), head.lower()).ratio()
            if r > score:
                best, score = i, r
        if best is not None:
            lines[best] = prefix + lines[best].strip()
    return "\n".join(lines)


def _fit(im: Image.Image, max_side: int | None) -> Image.Image:
    if not max_side or max(im.size) <= max_side:
        return im
    scale = max_side / max(im.size)
    return im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)


def _blocks_markdown(recs: list[dict]) -> str:
    parts = []
    for rec in recs:
        label, text = rec["label"], rec.get("text", "")
        if rec.get("image"):
            parts.append(f"![{label}]({rec['image']})")
            continue
        if not text:
            continue
        if rec.get("task") == "formula" and "$" not in text:
            text = f"$$\n{text}\n$$"
        elif label in TITLE:
            text = "# " + text.lstrip("# ")
        elif label in HEADING:
            text = "## " + text.lstrip("# ")
        parts.append(text)
    return "\n\n".join(parts)
