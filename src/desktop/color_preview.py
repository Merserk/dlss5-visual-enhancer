"""Low-latency, full-resolution still previews for the Coloring LUT card.

The export pipeline bakes a .cube and writes lossless intermediates.  A still
preview can evaluate the same grading function at the source pixels directly:
this avoids the cube bake, intermediate files, and video encoder.  The source
frame and its unadjusted LUT sample are reused while sliders move.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..core.jobs import Cancelled
from ..core.paths import FFMPEG
from ..neural_rendering.image.decoder import decode_image
from ..settings.models import UISettings
from .coloring import grade_lut_rgb, load_cube_lut, sample_cube_lut


MAX_PREVIEW_PIXELS = 8_500_000
_MAX_CACHE_BYTES = 320 * 1024 * 1024
_lock = threading.RLock()


@dataclass
class _CachedFrame:
    key: tuple
    rgba: np.ndarray
    rgb: np.ndarray
    lut_key: tuple | None = None
    sampled: np.ndarray | None = None


_cached_frame: _CachedFrame | None = None


class FastPreviewUnavailable(ValueError):
    """Use the file-backed preview when this source cannot use the RAM path."""


def _source_key(source: str | Path, mode: str, start_seconds: float,
                size: tuple[int, int] | None) -> tuple:
    path = Path(source).resolve()
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size, mode,
            round(float(start_seconds), 6) if mode == "Video" else 0,
            size if mode == "Video" else None)


def _video_frame(path: Path, start_seconds: float, size: tuple[int, int]) -> np.ndarray:
    width, height = size
    if width <= 0 or height <= 0 or width * height > MAX_PREVIEW_PIXELS:
        raise FastPreviewUnavailable("Video dimensions are unavailable for the fast preview.")
    # Match the seek used by the lossless clip extractor.  ffmpeg converts the
    # decoded frame to 16-bit RGBA, so 10-bit source color is not truncated.
    start = max(0.0, float(start_seconds) - 0.002)
    fast_seek = max(0.0, start - 2.0)
    accurate_seek = max(0.0, start - fast_seek)
    command = [str(FFMPEG), "-hide_banner", "-loglevel", "error",
               "-ss", f"{fast_seek:.6f}", "-i", str(path),
               "-ss", f"{accurate_seek:.6f}", "-frames:v", "1", "-an",
               "-f", "rawvideo", "-pix_fmt", "rgba64le", "pipe:1"]
    result = subprocess.run(
        command, capture_output=True, timeout=30, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip()
                           or "Could not decode the preview frame.")
    if len(result.stdout) != width * height * 8:
        raise FastPreviewUnavailable("Decoded frame dimensions differ from the source metadata.")
    return np.frombuffer(result.stdout, dtype="<u2").reshape(height, width, 4)


def _frame(key: tuple, size: tuple[int, int] | None) -> _CachedFrame:
    global _cached_frame
    with _lock:
        if _cached_frame is not None and _cached_frame.key == key:
            return _cached_frame
    path = Path(key[0])
    rgba = decode_image(path).rgba if key[3] == "Image" else _video_frame(path, key[4], size)
    if rgba.shape[0] * rgba.shape[1] > MAX_PREVIEW_PIXELS:
        raise FastPreviewUnavailable("This image is too large for the in-memory preview.")
    maximum = np.iinfo(rgba.dtype).max
    rgb = rgba[..., :3].astype(np.float32) / float(maximum)
    frame = _CachedFrame(key, rgba, rgb)
    if rgba.nbytes + rgb.nbytes <= _MAX_CACHE_BYTES:
        with _lock:
            _cached_frame = frame
    return frame


def _lut_sample(frame: _CachedFrame, path: str) -> np.ndarray:
    if not path:
        return frame.rgb
    lut_path = Path(path).resolve()
    stat = lut_path.stat()
    key = (str(lut_path), stat.st_mtime_ns, stat.st_size)
    with _lock:
        if frame.lut_key == key and frame.sampled is not None:
            return frame.sampled
    lut = load_cube_lut(lut_path)
    # Keep temporary trilinear arrays bounded for large source frames.
    sampled = np.empty_like(frame.rgb)
    flattened_input = frame.rgb.reshape(-1, 3)
    flattened_output = sampled.reshape(-1, 3)
    for offset in range(0, len(flattened_input), 262_144):
        end = min(len(flattened_input), offset + 262_144)
        flattened_output[offset:end] = sample_cube_lut(flattened_input[offset:end], lut)
    if frame.rgba.nbytes + frame.rgb.nbytes + sampled.nbytes <= _MAX_CACHE_BYTES:
        with _lock:
            frame.lut_key = key
            frame.sampled = sampled
    return sampled


def render_color_still_preview(source: str | Path, settings: UISettings, mode: str,
                               *, start_seconds: float = 0.0,
                               video_size: tuple[int, int] | None = None,
                               controller=None, progress=None) -> tuple[np.ndarray, str]:
    """Grade an image or one video frame in memory at source resolution.

    The selected export LUT resolution affects .cube sampling, not this
    evaluation.  Evaluating the grading function directly is at least as
    accurate as sampling its baked 33/65/129-point approximation.
    """
    if mode not in {"Image", "Video"}:
        raise ValueError("Unsupported coloring preview mode.")
    if mode == "Video" and video_size is None:
        raise ValueError("Video dimensions are required for the fast preview.")
    key = _source_key(source, mode, start_seconds, video_size)
    if controller is not None and controller.cancel.is_set():
        raise Cancelled("Preview superseded by newer settings.")
    frame = _frame(key, video_size)
    if progress is not None:
        progress(0.35, "Coloring: source frame ready")
    if controller is not None and controller.cancel.is_set():
        raise Cancelled("Preview superseded by newer settings.")
    if settings.lut_mix == 0 and settings.lut_path:
        load_cube_lut(settings.lut_path)
    sampled = frame.rgb if settings.lut_mix == 0 else _lut_sample(frame, settings.lut_path)
    if controller is not None and controller.cancel.is_set():
        raise Cancelled("Preview superseded by newer settings.")
    maximum = np.iinfo(frame.rgba.dtype).max
    result = np.empty_like(frame.rgba)
    source_rgb = frame.rgb.reshape(-1, 3)
    sampled_rgb = sampled.reshape(-1, 3)
    output_pixels = result.reshape(-1, 4)
    for offset in range(0, len(source_rgb), 262_144):
        if controller is not None and controller.cancel.is_set():
            raise Cancelled("Preview superseded by newer settings.")
        end = min(len(source_rgb), offset + 262_144)
        graded = grade_lut_rgb(source_rgb[offset:end], sampled_rgb[offset:end], settings)
        output_pixels[offset:end, :3] = np.rint(
            np.clip(graded, 0.0, 1.0) * maximum).astype(frame.rgba.dtype)
    result[..., 3] = frame.rgba[..., 3]
    if progress is not None:
        progress(1.0, "Coloring preview complete")
    return result, ""
