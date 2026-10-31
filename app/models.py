"""Model registry and download/verify CLI.

All weights live in `models/hf` (Hugging Face cache layout: content-addressed
blobs + per-revision snapshots). Pinning a revision makes updates atomic: a new
revision downloads into a new snapshot and the old one keeps working until
`prune` removes it.

    python -m app.models status      # what is installed, sizes, integrity
    python -m app.models download    # fetch missing models (only network step)
    python -m app.models prune       # delete repos/revisions not in the registry
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from app import config


@dataclass(frozen=True)
class ModelSpec:
    key: str
    repo: str
    revision: str
    runtime: str
    fmt: str
    license: str
    purpose: str
    convert_to: str | None = None  # MLX conversion target dir under models/, if the repo isn't MLX-native
    candidate: bool = False        # benchmark-only alternative: not downloaded by default, removed by prune


# Selection rationale and measurements: benchmarks/REPORT.md.
REGISTRY: dict[str, ModelSpec] = {
    s.key: s
    for s in [
        ModelSpec("paddleocr-vl-4bit", "mlx-community/PaddleOCR-VL-1.6-4bit", "c8987b277bdd8aaec8bd265d154f391bd03ad165",
                  "mlx-vlm (Metal)", "MLX 4-bit", "Apache-2.0", "OCR avanzato per pagine complesse (blocchi di layout)"),
        ModelSpec("pp-doclayout-v3", "PaddlePaddle/PP-DocLayoutV3_safetensors", "e4f489dc536556fc1be02b973ba06756e0d4a2ac",
                  "mlx-vlm (Metal)", "safetensors fp32 -> MLX fp32", "Apache-2.0", "Layout + ordine di lettura per PaddleOCR-VL",
                  convert_to="mlx/pp-doclayout-v3"),
        ModelSpec("glm-ocr-4bit", "mlx-community/GLM-OCR-4bit", "97f587506984cc92fa69b2694b4128e53db6b081",
                  "mlx-vlm (Metal)", "MLX 4-bit", "MIT", "Fallback per pagine difficili (pagina intera, 1024 px)"),
        ModelSpec("docling-layout", "docling-project/docling-layout-heron", "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8",
                  "docling (torch/MPS)", "safetensors", "Apache-2.0", "Docling: analisi layout"),
        ModelSpec("docling-tableformer", "docling-project/docling-models", "v2.3.0",
                  "docling (torch/MPS)", "safetensors", "Apache-2.0", "Docling: struttura tabelle"),
        # Benchmark candidates that lost (REPORT.md): same quality, slower and bigger.
        ModelSpec("paddleocr-vl", "PaddlePaddle/PaddleOCR-VL-1.6", "c5630abae1d940eafe0697512a0325494b02ab42",
                  "mlx-vlm (Metal)", "safetensors bf16", "Apache-2.0", "candidato benchmark", candidate=True),
        ModelSpec("glm-ocr", "mlx-community/GLM-OCR-bf16", "24f15402e83baa0a80eeeaecf5480e172abc6f2e",
                  "mlx-vlm (Metal)", "MLX bf16", "MIT", "candidato benchmark", candidate=True),
        ModelSpec("glm-ocr-8bit", "mlx-community/GLM-OCR-8bit", "350a346bea43468bcd202abb833f7d32e9a17237",
                  "mlx-vlm (Metal)", "MLX 8-bit", "MIT", "candidato benchmark", candidate=True),
    ]
}
DEFAULT_KEYS = [k for k, s in REGISTRY.items() if not s.candidate]


def _hub_dir() -> Path:
    return config.HF_HOME / "hub"


def _repo_dir(repo: str) -> Path:
    return _hub_dir() / f"models--{repo.replace('/', '--')}"


def du(path: Path) -> int:
    """Real on-disk size, counting each blob once (snapshots are symlinks)."""
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    # Snapshot entries are symlinks into (possibly shared) blob stores: count each real file once.
    real = {os.path.realpath(p) for p in path.rglob("*") if p.is_file()}
    return sum(os.path.getsize(r) for r in real)


def local_path(key: str) -> Path | None:
    """Snapshot dir (or converted dir) for a registered model, or None if missing."""
    spec = REGISTRY[key]
    if spec.convert_to:
        p = config.MODELS_DIR / spec.convert_to
        return p if (p / "model.safetensors").exists() else None
    from huggingface_hub import snapshot_download
    try:
        return Path(snapshot_download(spec.repo, revision=_full_revision(spec), local_files_only=True))
    except Exception:
        return None


def _full_revision(spec: ModelSpec) -> str:
    """Resolve a short sha to the full one recorded in the local cache refs/snapshots."""
    snaps = _repo_dir(spec.repo) / "snapshots"
    if snaps.exists():
        for s in snaps.iterdir():
            if s.name.startswith(spec.revision):
                return s.name
    ref = _repo_dir(spec.repo) / "refs" / spec.revision
    return ref.read_text().strip() if ref.exists() else spec.revision


def verify(key: str) -> tuple[bool, str]:
    """Check every snapshot file resolves to a blob of the size the hub recorded at download time."""
    p = local_path(key)
    if p is None:
        return False, "missing"
    broken = [f.name for f in p.rglob("*") if f.is_symlink() and not f.exists()]
    if broken:
        return False, f"broken links: {broken}"
    if not any(p.rglob("*.safetensors")):
        return False, "no weights"
    return True, "ok"


def status() -> list[dict]:
    rows = []
    for key, spec in REGISTRY.items():
        ok, msg = verify(key)
        size = du(config.MODELS_DIR / spec.convert_to) if spec.convert_to else du(_repo_dir(spec.repo))
        if spec.convert_to:
            size += du(_repo_dir(spec.repo))
        rows.append(dict(key=key, repo=spec.repo, revision=spec.revision, runtime=spec.runtime, format=spec.fmt,
                         license=spec.license, purpose=spec.purpose, size_mb=round(size / config.MB, 1),
                         installed=ok, detail=msg))
    return rows


def download(keys: list[str]) -> None:
    os.environ["HF_HUB_OFFLINE"] = "0"
    import huggingface_hub
    from huggingface_hub import HfApi, snapshot_download
    huggingface_hub.constants.HF_HUB_OFFLINE = False
    api = HfApi()
    for key in keys:
        spec = REGISTRY[key]
        if key.startswith("docling-"):
            continue  # fetched by docling itself below, with docling's own pinned revisions
        if verify(key)[0]:
            print(f"[skip] {key}: already installed")
            continue
        info = api.model_info(spec.repo, revision=spec.revision, files_metadata=True)
        need = sum(s.size or 0 for s in info.siblings)
        free = shutil.disk_usage(config.MODELS_DIR).free
        if free - need < config.MIN_FREE_DISK:
            sys.exit(f"[abort] {key}: needs {need / config.GB:.2f} GB, only {free / config.GB:.2f} GB free")
        print(f"[get ] {key}: {spec.repo}@{info.sha[:8]} ({need / config.GB:.2f} GB)")
        path = Path(snapshot_download(spec.repo, revision=info.sha))
        if spec.convert_to:
            from mlx_vlm.models.pp_doclayout_v3.convert import convert
            convert(str(path), str(config.MODELS_DIR / spec.convert_to), dtype="float32")
    if any(k.startswith("docling-") for k in keys):
        print("[get ] docling layout + tableformer")
        # Docling asks for the layout model at "main": fetch the pinned revisions, point "main" at
        # the pin, then initialise Docling offline so it can't pull a newer, unmeasured main.
        for key in ("docling-layout", "docling-tableformer"):
            snapshot_download(REGISTRY[key].repo, revision=REGISTRY[key].revision)
        heron = REGISTRY["docling-layout"]
        (_repo_dir(heron.repo) / "refs").mkdir(exist_ok=True)
        (_repo_dir(heron.repo) / "refs" / "main").write_text(heron.revision)
        os.environ["HF_HUB_OFFLINE"] = "1"
        huggingface_hub.constants.HF_HUB_OFFLINE = True
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
        opts = PdfPipelineOptions(do_ocr=False, do_table_structure=True)
        DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}) \
            .initialize_pipeline(InputFormat.PDF)


def prune(dry_run: bool) -> None:
    """Remove repos/revisions under models/hf the app doesn't use (incl. benchmark candidates)."""
    keep = {_repo_dir(s.repo).name: _full_revision(s) for s in REGISTRY.values() if not s.candidate}
    for repo in sorted(_hub_dir().glob("models--*")):
        if repo.name not in keep:
            print(f"[{'would remove' if dry_run else 'remove'}] {repo.name} ({du(repo) / config.MB:.0f} MB)")
            if not dry_run:
                shutil.rmtree(repo)
            continue
        # Docling-managed repos use docling's own pins; leave their revisions alone.
        if repo.name.startswith("models--docling-project"):
            continue
        for snap in (repo / "snapshots").iterdir():
            if snap.name != keep[repo.name]:
                print(f"[{'would remove' if dry_run else 'remove'}] {repo.name}@{snap.name[:8]}")
                if not dry_run:
                    shutil.rmtree(snap)
    # Drop blobs (per-repo or the hub-wide shared store) that no snapshot points to anymore.
    # A shared blob is "<hash>" plus "<hash>.lock"/"<hash>.refs": keep or delete them together.
    used = {os.path.realpath(f) for f in _hub_dir().glob("models--*/snapshots/**/*") if f.is_symlink()}
    groups: dict[str, list[Path]] = {}
    for f in [*_hub_dir().glob("models--*/blobs/*"), *_hub_dir().glob("blobs/*/*")]:
        if f.is_file() and not f.is_symlink():
            groups.setdefault(os.path.realpath(f).removesuffix(".lock").removesuffix(".refs"), []).append(f)
    for base, files in groups.items():
        if base in used:
            continue
        size = sum(f.stat().st_size for f in files) / config.MB
        print(f"[{'would remove' if dry_run else 'remove'}] unreferenced blob {Path(base).name[:16]} ({size:.0f} MB)")
        if not dry_run:
            for f in files:
                f.unlink()


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m app.models")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    d = sub.add_parser("download")
    d.add_argument("keys", nargs="*", default=DEFAULT_KEYS, help=f"default: {' '.join(DEFAULT_KEYS)}")
    p = sub.add_parser("prune")
    p.add_argument("--yes", action="store_true", help="actually delete (default: dry run)")
    a = ap.parse_args()
    if a.cmd == "status":
        total = 0.0
        for r in status():
            if REGISTRY[r["key"]].candidate and not r["installed"]:
                continue
            total += r["size_mb"]
            print(f"{r['key']:20s} {'OK ' if r['installed'] else 'NO '} {r['size_mb']:8.1f} MB  {r['format']:28s} "
                  f"{r['runtime']:20s} {r['repo']}@{r['revision'][:8]}  {r['detail']}")
        print(f"{'total':20s}     {total:8.1f} MB")
    elif a.cmd == "download":
        download(a.keys)
    else:
        prune(dry_run=not a.yes)


if __name__ == "__main__":
    main()
