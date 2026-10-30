"""Shared engine types."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from app import config
from app.resources import EngineResources, manager


@dataclass
class PageOutput:
    page_no: int                  # 1-based; 0 for formats without pages (DOCX, XLSX...)
    markdown: str
    engine: str
    quality: float | None = None  # 0..1 from app.quality, filled by the pipeline
    data: dict[str, Any] = field(default_factory=dict)  # engine-specific structure for the JSON export


class Engine:
    """An engine owns at most one set of loaded weights and declares what it costs.

    Subclasses implement `_load()` (return the loaded object) and `_unload(obj)`; callers
    use `run(fn, working_mb)` which takes a memory lease, loads on demand and calls fn(model).
    """

    name: str = "engine"
    resources: EngineResources = EngineResources(0, 256, 1)

    def __init__(self) -> None:
        self._model: Any = None
        self._load_lock = threading.Lock()

    def available(self) -> tuple[bool, str]:
        return True, ""

    def _load(self) -> Any:
        return None

    def _unload(self, model: Any) -> None:
        pass

    def unload(self) -> None:
        with self._load_lock:
            if self._model is not None:
                model, self._model = self._model, None
                self._unload(model)

    def run(self, fn, working_mb: int | None = None, cancelled=lambda: False):
        r = self.resources
        rm = manager()
        with rm.lease(self.name, r.model_memory_mb * config.MB,
                      (working_mb or r.estimated_working_memory_mb) * config.MB, r.max_parallel_jobs, cancelled):
            with self._load_lock:
                if self._model is None:
                    self._model = self._load()
                    rm.model_now_resident(self.name, r.model_memory_mb * config.MB, self.unload)
                model = self._model
            return fn(model)
