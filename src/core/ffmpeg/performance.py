"""Bounded encoder-probe caching and explicit NVENC speed/quality controls."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path

_CACHE: OrderedDict[tuple, float] = OrderedDict()
_LOCK = threading.Lock()
_SUCCESS_TTL_SECONDS = 60.0
_MAX_ENTRIES = 128


def nvenc_preset() -> str:
    """Normal launch keeps p6; the optional 4090 launcher defaults to p4."""
    preset = os.environ.get("DLSS5_NVENC_PRESET", "p6").strip().lower()
    if preset not in {f"p{i}" for i in range(1, 8)}:
        raise ValueError("DLSS5_NVENC_PRESET must be p1 through p7 (default p6).")
    return preset


def clear_encoder_probe_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def probe_encoder(executable: Path, codec: str, width: int, height: int,
                  gpu_ordinal: int | None = None) -> bool:
    """Reuse recent successes only; errors/timeouts are always retried.

    Resolution, encoder, CUDA ordinal, CUDA visibility/order, and binary
    identity are part of the key. A successful probe is not a reservation;
    the actual encoder still checks hardware availability when it starts.
    """
    if width <= 0 or height <= 0:
        raise ValueError("Encoder probe dimensions must be positive.")
    if gpu_ordinal is not None and (isinstance(gpu_ordinal, bool) or not isinstance(gpu_ordinal, int) or gpu_ordinal < 0):
        raise ValueError("CUDA ordinal must be nonnegative or None.")
    path = Path(executable).resolve()
    try:
        stat = path.stat()
    except OSError:
        return False
    key = (str(path), stat.st_size, stat.st_mtime_ns, codec, width, height,
           gpu_ordinal, os.environ.get("CUDA_VISIBLE_DEVICES"),
           os.environ.get("CUDA_DEVICE_ORDER"))
    # Serialize duplicate startup probes as well as protecting the small LRU.
    with _LOCK:
        now = time.monotonic()
        if _CACHE.get(key, 0.0) > now:
            _CACHE.move_to_end(key)
            return True
        _CACHE.pop(key, None)
        command = [str(path), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                   f"color=size={width}x{height}:rate=1", "-frames:v", "1",
                   "-c:v", codec]
        if gpu_ordinal is not None:
            command += ["-gpu", str(gpu_ordinal)]
        command += ["-f", "null", "-"]
        try:
            success = subprocess.run(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
        if success:
            _CACHE[key] = time.monotonic() + _SUCCESS_TTL_SECONDS
            while len(_CACHE) > _MAX_ENTRIES:
                _CACHE.popitem(last=False)
        return success
