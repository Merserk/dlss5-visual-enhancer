from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from .gpu_detection import clear_gpu_detection_cache, detect_gpus


def gpu_choice_label(gpu: dict[str, Any]) -> str:
    memory_gib = float(gpu.get("memory_mb", 0)) / 1024
    location = gpu.get("pci_bus_id") or f"index {gpu.get('index', '?')}"
    return f"{gpu.get('name', 'NVIDIA GPU')} | {memory_gib:.0f} GB | PCI {location}"


def preferred_gpu_uuid(gpu_uuid: str = "auto") -> str:
    """Explicit UI selections win; the launcher can pin Automatic by UUID."""
    if gpu_uuid != "auto":
        return gpu_uuid
    return os.environ.get("DLSS5_PREFERRED_GPU_UUID", "").strip() or "auto"


def gpu_preference_key(gpu: dict[str, Any]) -> tuple[int, int, str]:
    """Capacity-first Automatic policy, not a universal GPU speed benchmark.

    On the target 4090/2080 Ti system this prefers the 24 GiB compute adapter,
    regardless of display attachment or enumeration order. UUID breaks ties.
    """
    return (int(gpu.get("memory_mb") or 0), int(bool(gpu.get("ai_compatible"))),
            str(gpu.get("uuid") or ""))


def resolve_ai_gpu(gpus: tuple[dict[str, Any], ...], gpu_uuid: str = "auto") -> dict[str, Any]:
    gpu_uuid = preferred_gpu_uuid(gpu_uuid)
    compatible = [gpu for gpu in gpus if gpu.get("ai_compatible")]
    if gpu_uuid != "auto":
        selected = next((gpu for gpu in compatible if gpu.get("uuid") == gpu_uuid), None)
        if selected is not None:
            return dict(selected)
        raise RuntimeError("The selected AI Processing GPU is unavailable or incompatible.")
    if not compatible:
        details = "; ".join(
            str(gpu.get("compatibility_error")) for gpu in gpus if gpu.get("compatibility_error")
        )
        message = "No supported NVIDIA RTX GPU was detected."
        raise RuntimeError(f"{message} {details}" if details else message)
    return dict(max(compatible, key=gpu_preference_key))


def resolve_runtime_ai_gpu(
    gpus: tuple[dict[str, Any], ...],
    runtime_bundle: dict[str, Any],
    gpu_uuid: str = "auto",
) -> dict[str, Any]:
    """Resolve any NVIDIA RTX GPU; native runtimes decide actual feature support."""
    del runtime_bundle
    return resolve_ai_gpu(gpus, gpu_uuid)


@lru_cache(maxsize=32)
def _detect_gpu_cached(gpu_uuid: str = "auto") -> dict:
    return resolve_ai_gpu(detect_gpus(), gpu_uuid)


def detect_gpu(gpu_uuid: str = "auto") -> dict:
    return dict(_detect_gpu_cached(preferred_gpu_uuid(gpu_uuid)))


def _clear_gpu_detection_cache() -> None:
    _detect_gpu_cached.cache_clear()
    clear_gpu_detection_cache()


# Publicly named version for the new module boundary.
clear_gpu_selection_cache = _clear_gpu_detection_cache


detect_gpu.cache_clear = _clear_gpu_detection_cache  # type: ignore[attr-defined]
