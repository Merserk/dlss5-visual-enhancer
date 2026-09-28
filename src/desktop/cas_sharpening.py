"""File-backed Vulkan CAS and NIS sharpening stages."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np

from ..core import ffmpeg
from ..core.cache_video import cache_ffmpeg_args, cache_video_format
from ..core.paths import FFMPEG
from ..core.jobs import Cancelled, JobController
from ..neural_rendering.image.decoder import decode_image
from ..neural_rendering.image.encoder import _encode_image
from ..neural_rendering.image.models import ImageConversionOptions
from ..settings.models import UISettings
from ..core.ffmpeg.sharpening import sharpen_rgb, sharpening_filter
from ..core.ffmpeg.vulkan import run as run_vulkan


def _selected_sharpen(pixels: np.ndarray, settings: UISettings, *, transfer: str,
                      alpha: np.ndarray | None = None,
                      controller: JobController | None = None) -> np.ndarray:
    if settings.sharpening_method == "NVIDIA NIS":
        return sharpen_rgb(pixels, settings.cas_sharpness, method="NVIDIA NIS",
                               alpha=alpha, controller=controller)
    if settings.sharpening_method == "AMD CAS":
        return sharpen_rgb(pixels, settings.cas_sharpness, transfer=transfer,
                           alpha=alpha, controller=controller)
    raise ValueError(f"Unknown sharpening method: {settings.sharpening_method!r}.")


def sharpen_image(source: Path, settings: UISettings, directory: Path,
                  controller: JobController, progress: Callable) -> Path:
    if settings.cas_sharpness == 0:
        progress(1.0, "Sharpening complete")
        return source
    decoded = decode_image(source)
    progress(0.1, "Sharpening image")
    rgba = decoded.rgba
    rgba[..., :3] = _selected_sharpen(rgba[..., :3], settings, transfer="srgb",
                                      alpha=decoded.alpha, controller=controller)
    if controller.cancel.is_set():
        raise Cancelled("Render stopped by user.")
    output = directory / "sharpened.tiff"
    progress(0.85, "Saving sharpened image")
    _encode_image(output, rgba,
                  ImageConversionOptions(output_format="TIFF", preserve_metadata=False),
                  {}, generate_preview=False, controller=controller,
                  has_transparency=decoded.alpha is not None)
    progress(1.0, "Sharpening complete")
    return output


def sharpen_video(source: Path, settings: UISettings, directory: Path,
                  controller: JobController, progress: Callable) -> Path:
    if settings.cas_sharpness == 0:
        progress(1.0, "Sharpening complete")
        return source
    metadata = ffmpeg.probe_video(source, count_mode="metadata", controller=controller)
    if metadata.get("hdr"):
        raise ValueError("Sharpening requires SDR video at this position. Move it before HDR conversion.")
    transfer = {"iec61966-2-1": "srgb", "linear": "linear"}.get(
        metadata.get("color_transfer"), "bt709")
    output = directory / ("sharpened" + cache_video_format(settings.cache_codec)[2])
    progress(0.0, "Sharpening video on Vulkan")
    graph = "ve_gpu," + sharpening_filter(settings.sharpening_method, settings.cas_sharpness, transfer)
    command = [str(FFMPEG), "-v", "error", "-y", "-i", str(source),
               "-map", "0:v:0", "-map_metadata", "0", "-an", "-vf", graph,
               *cache_ffmpeg_args(settings.cache_codec, metadata),
               "-fps_mode", "passthrough", str(output)]
    run_vulkan(command, controller, "Vulkan sharpening", selection=settings.ffmpeg_device,
               progress=progress, metadata=metadata)
    progress(1.0, "Sharpening complete")
    return output
