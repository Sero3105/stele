"""Batch conversion from the terminal, without the web app.

    .venv/bin/python -m app.convert cartella/ file.pdf --mode auto --out risultati/ [--rag] [--split] [--pages 1-5]

Each document lands in <out>/<its folder>/<name>/ (name.md, assets/, metadata.json), the same
layout as the ZIP download. Files are converted one at a time; a failure is reported and the
batch continues; an e-mail's attachments are converted too, in '<mail> (allegati)/'. Exit
status 1 if any file failed.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from app import config, router
from app.export import folder_names
from app.pipeline import Context, convert_file, parse_pages


def collect(inputs: list[Path]) -> list[tuple[Path, Path, str]]:
    """(file, folder its relative path is relative to, relative path); hidden files skipped."""
    out = []
    for item in inputs:
        if item.is_dir():
            for f in sorted(item.rglob("*")):
                rel = f.relative_to(item.parent)
                if f.is_file() and not any(p.startswith(".") for p in rel.parts):
                    out.append((f, item.parent, rel.as_posix()))
        elif item.is_file():
            out.append((item, item.parent, item.name))
        else:
            print(f"non trovato: {item}", file=sys.stderr)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.convert", description="Converte documenti in Markdown.")
    ap.add_argument("inputs", nargs="+", type=Path, help="file o cartelle")
    ap.add_argument("--out", type=Path, required=True, help="cartella dei risultati")
    ap.add_argument("--mode", choices=router.MODES, default=router.AUTO)
    ap.add_argument("--rag", action="store_true", help="marcatori di pagina e chunks.jsonl")
    ap.add_argument("--split", action="store_true", help="una nota per capitolo e una nota indice")
    ap.add_argument("--pages", default="", help="solo queste pagine dei PDF, es. 1-5,8")
    args = ap.parse_args(argv)
    if args.pages:
        try:
            parse_pages(args.pages)
        except ValueError as exc:
            ap.error(str(exc))

    files = collect(args.inputs)
    failed = 0
    with tempfile.TemporaryDirectory(dir=config.WORK, prefix="cli-") as tmp:
        i = 0
        while i < len(files):  # e-mail attachments are appended while going
            folders = folder_names([{"id": k, "rel_path": rel} for k, (_, _, rel) in enumerate(files)])
            src, root, rel = files[i]
            dest, tag = args.out / folders[i], f"[{i + 1}/{len(files)}] {rel}"
            i += 1
            if dest.exists():
                print(f"{tag}: salto, {dest} esiste già", file=sys.stderr)
                failed += 1
                continue
            with tempfile.TemporaryDirectory(dir=tmp) as work:
                ctx = Context(out_dir=dest, work_dir=Path(work), name=src.name, mode=args.mode, upload_root=root,
                              rag=args.rag, split=args.split, pages=args.pages)
                try:
                    s = convert_file(src, ctx)
                except Exception as exc:  # one bad file never stops the batch
                    failed += 1
                    print(f"{tag}: errore: {exc}", file=sys.stderr)
                    continue
                for a in ctx.attachments:  # keep them past this file's work dir
                    kept = Path(tmp) / f"allegati-{i}" / a.name  # same name: it names the converted note
                    kept.parent.mkdir(exist_ok=True)
                    a.rename(kept)
                    files.append((kept, Path(tmp), f"{Path(rel).parent / (Path(rel).stem + ' (allegati)') / a.name}"))
            review = f", {len(s['review'])} pagine da controllare" if s["review"] else ""
            print(f"{tag} -> {s['markdown']} ({s['pages']} pagine, {s['seconds']} s{review})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
