"""Central configuration: paths, memory budget, limits.

Imported first by every entry point: it pins all Hugging Face downloads to the
project `models/` directory so nothing lands in the shared ~/.cache.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
HF_HOME = MODELS_DIR / "hf"

# Working data lives outside the project (macOS convention), never in the repository:
#   uploads/<job>/<relative path>   original files as uploaded
#   work/<job>/<file id>/           per-page checkpoints, internal report, assets in progress
#   results/<job>/<file id>/        final document: <name>.md, assets/, metadata.json
#   jobs.sqlite3                    persistent job state
# Page renders go to the system temp dir. ZIP archives are streamed, never stored.
STORAGE = Path(os.environ.get("STELE_STORAGE", Path.home() / "Library" / "Application Support" / "Stele"))
UPLOADS = STORAGE / "uploads"
WORK = STORAGE / "work"
RESULTS = STORAGE / "results"
DB_PATH = STORAGE / "jobs.sqlite3"
TEMP = Path(tempfile.gettempdir()) / "stele"
# Finished jobs (uploads, results, internal data) are deleted after this many days.
RETENTION_DAYS = float(os.environ.get("STELE_RETENTION_DAYS", "7"))
VISION_HELPER = ROOT / "native" / "vision_ocr"

os.environ.setdefault("HF_HOME", str(HF_HOME))
# Downloads only happen through `python -m app.models download`; at runtime the app must never phone home.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

GB = 1024**3
MB = 1024**2

# Hard ceiling for everything the app owns (models, workers, renders, buffers).
MEMORY_BUDGET = int(float(os.environ.get("STELE_MEMORY_BUDGET_GB", "20")) * GB)
# Stop admitting new work above this; leaves headroom for estimate error before the hard ceiling.
MEMORY_EMERGENCY = int(MEMORY_BUDGET * 0.85)

MAX_FILE_BYTES = 1 * GB
MIN_FREE_DISK = 2 * GB
HOST = "127.0.0.1"  # never configurable: the API has no authentication
PORT = int(os.environ.get("STELE_PORT", "8000"))

# Rendering resolution for OCR engines (pixels per PDF point * 72 = DPI).
OCR_DPI = 200

for d in (UPLOADS, WORK, RESULTS, TEMP):
    d.mkdir(parents=True, exist_ok=True)
