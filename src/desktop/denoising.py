"""Quality-first, lossless intermediate denoising for ordered image/video stages."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import cv2
import numpy as np
import tifffile

from ..core import ffmpeg
from ..core.jobs import Cancelled, JobController
from ..core.paths import FFMPEG
from ..neural_rendering.image.decoder import decode_image
from ..neural_rendering.image.encoder import _encode_image
from ..neural_rendering.image.models import ImageConversionOptions
from ..settings.models import UISettings


def _filters(settings: UISettings, *, video: bool, small: bool = False) -> str:
    """Run deblocking before NLM, so patch matching sees fewer 8×8 seams."""
    filters = ["format=gbrp10le" if video else "format=gbrp16le"]
    if settings.denoise_deblock and not small:
        # Weak edge-aware deblocking avoids flattening legitimate drawn lines.
        filters.append("deblock=filter=weak:block=8:alpha=0.05:beta=0.03:gamma=0.03:delta=0.03")
    if settings.denoise_strength:
        level = settings.denoise_strength
        strength = 1.0 + 0.01 * level + 0.0014 * level * level
        patch = 3 if small else 7
        search = 7 if small else 21
        filters.append(f"nlmeans=s={strength:.2f}:p={patch}:r={search}")
    if video and settings.denoise_temporal:
        # Adaptive temporal averaging rejects sufficiently different neighbours
        # at moving edges and scene changes. Keep the default threshold gentle.
        threshold_a = 0.006 + 0.00026 * settings.denoise_temporal
        threshold_b = threshold_a * 2.0
        window = 9 if settings.denoise_temporal > 65 else 5
        thresholds = ":".join(
            f"{plane}{suffix}={value:.5f}"
            for plane in range(3)
            for suffix, value in (("a", threshold_a), ("b", threshold_b))
        )
        filters.append(f"atadenoise=s={window}:{thresholds}")
    filters.append("format=gbrp10le" if video else "format=rgb48le")
    return ",".join(filters)


def denoise_image(source: Path, settings: UISettings, directory: Path,
                  controller: JobController, progress: Callable,
                  run_command: Callable) -> Path:
    if not settings.denoise_strength and not settings.denoise_deblock:
        progress(1.0, "Denoising complete")
        return source
    if controller.cancel.is_set():
        raise Cancelled("Render stopped by user.")
    decoded = decode_image(source)
    height, width = decoded.rgba.shape[:2]
    output = directory / "denoised.tiff"
    small = min(width, height) < 16
    original_rgb = np.ascontiguousarray(decoded.rgba[..., :3])
    progress(0.15, "Cleaning image noise")
    if decoded.rgba.dtype == np.uint8:
        # OpenCV's Lab-space colored NLM keeps fine luma detail better than a
        # single RGB strength. Equal luma/chroma settings avoid color smearing.
        filtered_rgb = original_rgb
        if settings.denoise_deblock and not small:
            filtered_rgb = _ffmpeg_image_filter(
                filtered_rgb, directory, controller, run_command,
                "format=gbrp16le,deblock=filter=weak:block=8:alpha=0.05:beta=0.03:gamma=0.03:delta=0.03,format=rgb48le",
            )
        if settings.denoise_strength:
            level = settings.denoise_strength
            luma = 1.0 + 0.024 * level + 0.0008 * level * level
            chroma = luma
            bgr = cv2.cvtColor(filtered_rgb, cv2.COLOR_RGB2BGR)
            cleaned = cv2.fastNlMeansDenoisingColored(
                bgr, None, luma, chroma, 3 if small else 7, 7 if small else 21,
            )
            filtered_rgb = cv2.cvtColor(cleaned, cv2.COLOR_BGR2RGB)
    else:
        # The colored OpenCV variant is 8-bit only. FFmpeg's 16-bit NLM avoids
        # reducing genuine high-depth image samples to 8-bit for this stage.
        filtered_rgb = _ffmpeg_image_filter(
            original_rgb, directory, controller, run_command,
            _filters(settings, video=False, small=small),
        )
    rgba = decoded.rgba.copy()
    rgba[..., :3] = filtered_rgb
    if decoded.alpha is not None:
        # Retain colors on nonopaque pixels so denoising cannot pull invisible
        # background colors across a transparency boundary.
        rgba[decoded.alpha < np.iinfo(rgba.dtype).max, :3] = decoded.rgba[
            decoded.alpha < np.iinfo(rgba.dtype).max, :3]
    if controller.cancel.is_set():
        raise Cancelled("Render stopped by user.")
    progress(0.85, "Saving denoised image")
    _encode_image(output, rgba,
                  ImageConversionOptions(output_format="TIFF", preserve_metadata=False),
                  {}, generate_preview=False, controller=controller,
                  has_transparency=decoded.alpha is not None)
    progress(1.0, "Denoising complete")
    return output


def _ffmpeg_image_filter(rgb: np.ndarray, directory: Path,
                         controller: JobController, run_command: Callable,
                         filter_graph: str) -> np.ndarray:
    height, width = rgb.shape[:2]
    input_path = directory / "denoise-input.tiff"
    filtered_path = directory / "denoise-filtered.tiff"
    tifffile.imwrite(input_path, rgb, photometric="rgb", compression=None, metadata=None)
    run_command(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(input_path), "-vf", filter_graph,
         "-frames:v", "1", str(filtered_path)],
        controller, "Image denoising",
    )
    filtered_bgr = cv2.imread(str(filtered_path), cv2.IMREAD_UNCHANGED)
    if (filtered_bgr is None or filtered_bgr.dtype != np.uint16
            or filtered_bgr.shape != (height, width, 3)):
        raise RuntimeError("Image denoising returned invalid 16-bit RGB pixels.")
    filtered_rgb = cv2.cvtColor(filtered_bgr, cv2.COLOR_BGR2RGB)
    if rgb.dtype == np.uint8:
        return ((filtered_rgb.astype(np.uint32) + 128) // 257).astype(np.uint8)
    return filtered_rgb


def denoise_video(source: Path, settings: UISettings, directory: Path,
                  controller: JobController, progress: Callable,
                  run_command: Callable) -> Path:
    if not (settings.denoise_strength or settings.denoise_deblock or settings.denoise_temporal):
        progress(1.0, "Denoising complete")
        return source
    metadata = ffmpeg.probe_video(source, count_mode="metadata", controller=controller)
    width, height = int(metadata["width"]), int(metadata["height"])
    color_args: list[str] = []
    for key, flag in (("color_primaries", "-color_primaries"),
                      ("color_transfer", "-color_trc")):
        value = metadata.get(key)
        if value and value != "unknown":
            color_args.extend((flag, str(value)))
    output = directory / "denoised.mkv"
    progress(0.1, "Cleaning video noise")
    run_command(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(source), "-map", "0:v:0", "-map_metadata", "0", "-an",
         "-vf", _filters(settings, video=True, small=min(width, height) < 16),
         "-c:v", "ffv1", "-level", "3", "-pix_fmt", "gbrp10le",
         "-fps_mode", "passthrough", *color_args, "-color_range", "pc",
         str(output)],
        controller, "Video denoising",
    )
    progress(1.0, "Denoising complete")
    return output
