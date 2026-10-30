"""ZIP export, streamed straight into the HTTP response (never written to disk).

One document:                      Several documents:
  nome.zip                           conversione.zip
  └── nome/                          ├── README.txt
      ├── nome.md                    ├── documento_01/  (same shape as on the left)
      ├── assets/…                   └── cartella/documento_02/…   (upload folders kept)
      └── metadata.json

Only the published result of each file goes in: no checkpoints, reports, logs or engine output.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from app.pipeline import safe_name

STORED = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip"}  # already compressed


class _Sink:
    """Write-only, non-seekable buffer: makes ZipFile emit a streamable archive (data descriptors)."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.pos = 0

    def write(self, b) -> int:
        self.buf += b
        self.pos += len(b)
        return len(b)

    def tell(self) -> int:
        return self.pos

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def stream_zip(entries: list[tuple[str, Path | bytes]]) -> Iterator[bytes]:
    """Yield a ZIP of (archive name, file path or bytes) in ~1 MB pieces; memory stays flat."""
    sink = _Sink()
    now = datetime.now().timetuple()[:6]
    with zipfile.ZipFile(sink, "w") as zf:
        for arc, src in entries:
            info = zipfile.ZipInfo(arc, now)
            info.compress_type = zipfile.ZIP_STORED if Path(arc).suffix.lower() in STORED else zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            if isinstance(src, bytes):
                zf.writestr(info, src)
                yield sink.drain()
                continue
            big = src.stat().st_size > 1 << 30
            with src.open("rb") as fh, zf.open(info, "w", force_zip64=big) as out:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    out.write(chunk)
                    yield sink.drain()
            yield sink.drain()
    yield sink.drain()


def document_entries(result_dir: Path, folder: str) -> list[tuple[str, Path]]:
    """<folder>/<stem>.md, <folder>/assets/*, <folder>/metadata.json of one published result."""
    md = next(result_dir.glob("*.md"))
    stem = folder.rsplit("/", 1)[-1]
    out = [(f"{folder}/{stem}.md", md)]
    assets = result_dir / "assets"
    if assets.is_dir():
        out += [(f"{folder}/assets/{p.name}", p) for p in sorted(assets.iterdir()) if p.is_file()]
    out.append((f"{folder}/metadata.json", result_dir / "metadata.json"))
    return out


def folder_names(files: list[dict]) -> dict[int, str]:
    """Archive folder per file: upload sub-folders kept, name = file stem; 'relazione.pdf' and
    'relazione.docx' side by side become 'relazione (pdf)' and 'relazione (docx)'."""
    out: dict[int, str] = {}
    taken: set[str] = set()
    stems: dict[tuple[str, str], int] = {}
    for f in files:
        rel = PurePosixPath(f["rel_path"])
        stems[(str(rel.parent), safe_name(rel.stem).lower())] = stems.get((str(rel.parent), safe_name(rel.stem).lower()), 0) + 1
    for f in files:
        rel = PurePosixPath(f["rel_path"])
        name = safe_name(rel.stem)
        if stems[(str(rel.parent), name.lower())] > 1:
            name = f"{name} ({rel.suffix.lstrip('.').lower() or 'file'})"
        base = name if str(rel.parent) == "." else f"{rel.parent}/{name}"
        candidate, i = base, 2
        while candidate.lower() in taken:
            candidate = f"{base} ({i})"
            i += 1
        taken.add(candidate.lower())
        out[f["id"]] = candidate
    return out


def readme(job: dict, folders: dict[int, str]) -> bytes:
    done = [f for f in job["files"] if f["state"] == "done"]
    failed = [f for f in job["files"] if f["state"] in ("error", "cancelled")]
    lines = [
        "Stele — documenti convertiti in Markdown",
        "=" * 40,
        "",
        f"Modalità di conversione: {job['mode']}",
        f"Documenti: {len(done)}" + (f" (non convertiti: {len(failed)})" if failed else ""),
        "",
        "Ogni cartella contiene:",
        "  <nome>.md       il documento in Markdown (apribile con Obsidian, VS Code, Typora...)",
        "  assets/         immagini e grafici collegati dal Markdown con percorsi relativi",
        "  metadata.json   informazioni tecniche: file sorgente, formato, pagine, modalità",
        "",
        "Contenuto:",
    ]
    for f in done:
        pages = (f.get("summary") or {}).get("pages")
        lines.append(f"  {folders[f['id']]}/  <-  {f['rel_path']}" + (f"  ({pages} pagine)" if pages else ""))
    if failed:
        lines += ["", "Non convertiti:"]
        lines += [f"  {f['rel_path']}: {f.get('error') or 'annullato'}" for f in failed]
    return ("\n".join(lines) + "\n").encode("utf-8")


def content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "replace").decode().replace("?", "_").replace('"', "")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
