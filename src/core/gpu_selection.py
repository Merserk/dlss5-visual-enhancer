from __future__ import annotations

from functools import lru_cache
from typing import Any

from .gpu_detection import clear_gpu_detection_cache, detect_gpus


def gpu_choice_label(gpu: dict[str, Any]) -> str:
    memory_gib = float(gpu.get("memory_mb", 0)) / 1024
    location = gpu.get("pci_bus_id") or f"index {gpu.get('index', '?')}"
    return f"{gpu.get('name', 'NVIDIA GPU')} | {memory_gib:.0f} GB | PCI {location}"


def resolve_ai_gpu(gpus: tuple[dict[str, Any], ...], gpu_uuid: str = "auto") -> dict[str, Any]:
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
    return dict(compatible[0])


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
    return _detect_gpu_cached(gpu_uuid)


def prefer_cuda_video(ffmpeg_device: str, ai_gpu_uuid: str = "auto",
                      video_gpu_uuid: str = "auto") -> bool:
    """Use the native CUDA video path for automatic or matching NVIDIA devices."""
    if ffmpeg_device == "cpu":
        return False
    if ffmpeg_device == "auto" and video_gpu_uuid == "auto":
        return True
    if ffmpeg_device != "auto" and not ffmpeg_device.startswith("vulkan:"):
        return False
    try:
        gpu_uuid = str(detect_gpu(ai_gpu_uuid)["uuid"])
    except (OSError, RuntimeError, ValueError, KeyError):
        return False
    if video_gpu_uuid not in {"auto", gpu_uuid}:
        return False
    return (ffmpeg_device == "auto" or
            ffmpeg_device[7:].lower() == gpu_uuid.removeprefix("GPU-").lower())


def _clear_gpu_detection_cache() -> None:
    _detect_gpu_cached.cache_clear()
    clear_gpu_detection_cache()


# Publicly named version for the new module boundary.
clear_gpu_selection_cache = _clear_gpu_detection_cache


detect_gpu.cache_clear = _clear_gpu_detection_cache  # type: ignore[attr-defined]
