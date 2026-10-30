"""Memory-aware admission control for the whole app.

Every conversion step asks for a Lease before running. A lease is granted only
if   max(measured footprint, committed estimate) + request  <=  emergency limit
and the OS still has free memory for it. Otherwise the caller waits (work is
queued, never dropped); idle models are evicted LRU to make room.

Footprint is the macOS `phys_footprint` (what Activity Monitor shows as
"Memory"), which — unlike RSS — includes Metal/MLX unified-memory buffers.
"""

from __future__ import annotations

import ctypes
import gc
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field

import psutil

from app import config

log = logging.getLogger(__name__)

_libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
_RUSAGE_INFO_V4 = 4
# rusage_info_v4 = 16-byte uuid followed by uint64 fields; indices below are into those fields.
_PHYS_FOOTPRINT, _LIFETIME_MAX_FOOTPRINT = 7, 28


def _rusage(pid: int) -> list[int]:
    buf = (ctypes.c_uint64 * 64)()
    if _libproc.proc_pid_rusage(pid, _RUSAGE_INFO_V4, ctypes.byref(buf)) != 0:
        raise OSError(ctypes.get_errno(), "proc_pid_rusage failed")
    return list(buf)[2:]  # skip the uuid (2 x uint64)


def footprint_split(pid: int | None = None) -> tuple[int, int]:
    """(this process, its children e.g. Apple Vision helpers) physical footprint in bytes."""
    pid = pid or os.getpid()
    children = 0
    for child in psutil.Process(pid).children(recursive=True):
        try:
            children += _rusage(child.pid)[_PHYS_FOOTPRINT]
        except (OSError, psutil.Error):
            pass  # the child exited between listing and reading
    return _rusage(pid)[_PHYS_FOOTPRINT], children


def footprint(pid: int | None = None) -> int:
    """Current physical footprint in bytes of this process plus its children (helpers)."""
    return sum(footprint_split(pid))


def mlx_memory() -> dict | None:
    """MLX's own accounting (Metal buffers), only if MLX is already loaded in this process."""
    mx = sys.modules.get("mlx.core")
    if mx is None:
        return None
    try:
        return {"active": mx.get_active_memory(), "peak": mx.get_peak_memory(), "cache": mx.get_cache_memory()}
    except Exception:  # API missing in this MLX version
        return None


def mps_memory() -> dict | None:
    """PyTorch MPS allocations (Docling's layout/table models), only if torch is loaded."""
    torch = sys.modules.get("torch")
    try:
        if torch is None or not torch.backends.mps.is_available():
            return None
        return {"allocated": torch.mps.current_allocated_memory(), "driver": torch.mps.driver_allocated_memory()}
    except Exception:
        return None


_gpu_cache: tuple[float, int | None] = (0.0, None)


def gpu_utilization() -> int | None:
    """GPU 'Device Utilization %' from the IOAccelerator registry (no root needed). Sampled at
    most every 2 s; None if the registry doesn't expose it."""
    global _gpu_cache
    now = time.monotonic()
    if now - _gpu_cache[0] < 2:
        return _gpu_cache[1]
    value = None
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-r", "-d", "1", "-c", "IOAccelerator"],
                             capture_output=True, text=True, timeout=1).stdout
        if m := re.search(r'"Device Utilization %"=(\d+)', out):
            value = int(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    _gpu_cache = (now, value)
    return value


def peak_footprint(pid: int | None = None) -> int:
    """Lifetime max physical footprint of a process (kernel-tracked, no sampling gaps)."""
    return _rusage(pid or os.getpid())[_LIFETIME_MAX_FOOTPRINT]


@dataclass(frozen=True)
class EngineResources:
    """Measured costs of an engine (see benchmarks/REPORT.md for where numbers come from)."""
    model_memory_mb: int           # resident once loaded (weights + runtime overhead)
    estimated_working_memory_mb: int  # extra peak per concurrent job
    max_parallel_jobs: int         # library/hardware limit, independent of memory


@dataclass
class _Resident:
    name: str
    size: int
    unload: Callable[[], None]
    last_used: float = field(default_factory=time.monotonic)


class ResourceManager:
    def __init__(self, budget: int = config.MEMORY_BUDGET, emergency: int = config.MEMORY_EMERGENCY,
                 idle_unload_s: float = 300.0):
        self.budget, self.emergency, self.idle_unload_s = budget, emergency, idle_unload_s
        self.baseline = footprint()  # interpreter + libraries, before any model
        self._cond = threading.Condition()
        self._models: dict[str, _Resident] = {}
        self._working: dict[int, tuple[str, int]] = {}  # lease id -> (engine, bytes)
        self._next_id = 0
        self.peak_seen = self.baseline
        self.waits = 0
        self._throttle_logged: dict[str, float] = {}
        threading.Thread(target=self._janitor, daemon=True, name="rm-janitor").start()

    # ---- accounting -------------------------------------------------------------------------
    def _active(self, engine: str) -> int:
        return sum(1 for e, _ in self._working.values() if e == engine)

    def committed(self) -> int:
        return (self.baseline + sum(m.size for m in self._models.values())
                + sum(b for _, b in self._working.values()))

    def snapshot(self) -> dict:
        with self._cond:
            main, children = footprint_split()
            fp = main + children
            self.peak_seen = max(self.peak_seen, fp)
            vm = psutil.virtual_memory()
            return {
                "footprint": fp, "footprint_main": main, "footprint_children": children,
                "peak_main_kernel": peak_footprint(), "mlx": mlx_memory(), "mps": mps_memory(),
                "committed": self.committed(), "budget": self.budget,
                "emergency": self.emergency, "peak": self.peak_seen, "system_total": vm.total,
                "system_available": vm.available,
                "models": [{"name": m.name, "size": m.size, "active": self._active(m.name),
                            "idle_s": round(time.monotonic() - m.last_used)} for m in self._models.values()],
                "active_jobs": [e for e, _ in self._working.values()],
                "waits": self.waits,
            }

    # ---- model residency ----------------------------------------------------------------------
    def is_loaded(self, name: str) -> bool:
        with self._cond:
            return name in self._models

    def _pop_idle_victim(self, keep: str | None):
        """Remove the least-recently-used idle model from the registry (caller holds the lock).

        The caller must unload() it AFTER releasing the lock: unload waits on the
        engine's load lock, whose holder registers the loaded model under this
        same lock — unloading under the lock deadlocks the whole manager."""
        idle = [m for m in self._models.values() if self._active(m.name) == 0 and m.name != keep]
        if not idle:
            return None
        victim = min(idle, key=lambda m: m.last_used)
        del self._models[victim.name]
        return victim

    def _janitor(self) -> None:
        while True:
            time.sleep(15)
            with self._cond:
                now = time.monotonic()
                victims = [m for m in list(self._models.values())
                           if self._active(m.name) == 0 and now - m.last_used > self.idle_unload_s]
                for m in victims:
                    del self._models[m.name]
            for m in victims:
                log.info("unloading %s after %.0fs idle", m.name, now - m.last_used)
                m.unload()
                gc.collect()
            if victims:
                with self._cond:
                    self._cond.notify_all()

    # ---- leases -----------------------------------------------------------------------------
    @contextmanager
    def lease(self, engine: str, model_bytes: int, working_bytes: int, max_parallel: int,
              cancelled: Callable[[], bool] = lambda: False):
        """Block until `engine` may run one more job; yield; release.

        `model_bytes` is charged only if the engine's model isn't resident yet; the engine must then
        call model_now_resident() once loaded so the cost moves from the lease to residency.
        """
        lid = self._acquire(engine, model_bytes, working_bytes, max_parallel, cancelled)
        try:
            yield
        finally:
            with self._cond:
                self._working.pop(lid, None)
                if (m := self._models.get(engine)):
                    m.last_used = time.monotonic()
                self._cond.notify_all()

    def _acquire(self, engine, model_bytes, working_bytes, max_parallel, cancelled) -> int:
        if working_bytes + model_bytes + self.baseline > self.budget:
            raise MemoryError(f"{engine}: job needs ~{(working_bytes + model_bytes) / config.GB:.1f} GB, "
                              f"over the {self.budget / config.GB:.0f} GB budget")
        waited = False
        while True:
            victim = None
            with self._cond:
                while True:
                    if cancelled():
                        raise InterruptedError("cancelled while queued")
                    running = self._active(engine)
                    load = 0 if engine in self._models else model_bytes
                    need = working_bytes + load
                    fp = footprint()
                    self.peak_seen = max(self.peak_seen, fp)
                    projected = max(fp, self.committed()) + need
                    os_ok = psutil.virtual_memory().available - need > 1 * config.GB
                    if running < max_parallel and projected <= self.emergency and os_ok:
                        break
                    if running < max_parallel:
                        victim = self._pop_idle_victim(keep=engine)
                        if victim is not None:
                            break  # unload it outside the lock, then re-check admission
                    if not self._working and running < max_parallel and projected <= self.budget:
                        # Nothing else running: allow up to the hard budget so a lone big job can progress.
                        break
                    if not waited:
                        self.waits += 1
                        waited = True
                        self._log_throttle(engine, projected, running, max_parallel)
                    self._cond.wait(timeout=1.0)
                if victim is None:
                    self._next_id += 1
                    # Model cost is held by the lease until model_now_resident() moves it to residency.
                    self._working[self._next_id] = (engine, need)
                    return self._next_id
            log.info("evicting idle model %s (%.0f MB)", victim.name, victim.size / config.MB)
            victim.unload()
            gc.collect()

    def _log_throttle(self, engine: str, projected: int, running: int, max_parallel: int) -> None:
        now = time.monotonic()
        if now - self._throttle_logged.get(engine, 0.0) < 60:
            return
        self._throttle_logged[engine] = now
        log.info("throttling %s: projected %.1f GB (emergency %.1f GB), running %d/%d",
                 engine, projected / config.GB, self.emergency / config.GB, running, max_parallel)

    def model_now_resident(self, engine: str, size: int, unload: Callable[[], None]) -> None:
        """Called by an engine right after it loaded weights inside a lease."""
        with self._cond:
            for lid, (e, b) in self._working.items():
                if e == engine and b > size:
                    self._working[lid] = (e, b - size)
                    break
            self._models[engine] = _Resident(engine, size, unload)
            self._cond.notify_all()


RM: ResourceManager | None = None


def manager() -> ResourceManager:
    global RM
    if RM is None:
        RM = ResourceManager()
    return RM
