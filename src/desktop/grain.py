"""File-backed image and video stages for the Grain processing card."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..core import ffmpeg
from ..core.cache_video import cache_ffmpeg_args, cache_video_format
from ..core.ffmpeg.grain import GrainOptions, grain_filter, grain_rgba
from ..core.ffmpeg.vulkan import run as run_vulkan
from ..core.jobs import Cancelled, JobController
from ..core.paths import FFMPEG
from ..neural_rendering.image.decoder import decode_image
from ..neural_rendering.image.encoder import _encode_image
from ..neural_rendering.image.models import ImageConversionOptions
from ..settings.models import UISettings


def grain_image(source: Path, settings: UISettings, directory: Path,
                controller: JobController, progress: Callable) -> Path:
    options = GrainOptions.from_settings(settings, video=False).validate()
    if controller.cancel.is_set():
        raise Cancelled("Render stopped by user.")
    if not options.amount:
        progress(1.0, "Grain complete")
        return source
    decoded = decode_image(source)
    progress(0.15, "Applying grain on Vulkan")
    rgba = grain_rgba(decoded.rgba, options, controller=controller)
    if controller.cancel.is_set():
        raise Cancelled("Render stopped by user.")
    output = directory / "grain.tiff"
    progress(0.85, "Saving image with grain")
    _encode_image(output, rgba,
                  ImageConversionOptions(output_format="TIFF", preserve_metadata=False),
                  {}, generate_preview=False, controller=controller,
                  has_transparency=decoded.alpha is not None)
    progress(1.0, "Grain complete")
    return output


def grain_video(source: Path, settings: UISettings, directory: Path,
                controller: JobController, progress: Callable) -> Path:
    options = GrainOptions.from_settings(settings).validate()
    if controller.cancel.is_set():
        raise Cancelled("Render stopped by user.")
    if not options.amount:
        progress(1.0, "Grain complete")
        return source
    metadata = ffmpeg.probe_video(source, count_mode="metadata", controller=controller)
    output = directory / ("grain" + cache_video_format(settings.cache_codec)[2])
    graph = "ve_gpu," + grain_filter(options, hdr=bool(metadata.get("hdr")))
    command = [str(FFMPEG), "-v", "error", "-y", "-i", str(source),
               "-map", "0:v:0", "-map_metadata", "0", "-an", "-vf", graph,
               *cache_ffmpeg_args(settings.cache_codec, metadata),
               "-fps_mode", "passthrough", "-enc_time_base:v", "demux", str(output)]
    progress(0.0, "Applying grain on Vulkan")
    run_vulkan(command, controller, "Vulkan grain", selection=settings.ffmpeg_device,
               progress=progress, metadata=metadata)
    progress(1.0, "Grain complete")
    return output
