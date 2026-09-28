"""Low-latency, full-resolution still previews for the Coloring LUT card.

The still path avoids lossless video intermediates. Source pixels are reused
while sliders move; the composed export-equivalent cube is reused while the
timeline moves. Both caches are bounded to one entry.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..core.ffmpeg.vulkan import prepare_command
from ..core.jobs import Cancelled
from ..core.paths import FFMPEG
from ..neural_rendering.image.decoder import decode_image
from ..settings.models import LUT_ADJUSTMENT_RANGES, UISettings
from .coloring import (apply_cube_lut_file, compose_cube_lut, identity_cube_lut,
                       load_cube_lut, write_composed_lut)
from .preview_cache import cache_key, file_identity


MAX_PREVIEW_PIXELS = 8_500_000
_MAX_CACHE_BYTES = 320 * 1024 * 1024
_lock = threading.RLock()


@dataclass
class _CachedFrame:
    key: tuple
    rgba: np.ndarray


_cached_frame: _CachedFrame | None = None


@dataclass
class _CachedLut:
    key: str
    directory: tempfile.TemporaryDirectory
    path: Path

    def __post_init__(self):
        weakref.finalize(self, self.directory.cleanup)


_cached_lut: _CachedLut | None = None


def _prepared_lut(settings: UISettings, controller=None) -> _CachedLut:
    global _cached_lut
    key = cache_key({"lut": file_identity(settings.lut_path) if settings.lut_path else None,
                     "resolution": settings.lut_resolution,
                     "adjustments": {field: getattr(settings, field) for field in LUT_ADJUSTMENT_RANGES}})
    with _lock:
        if _cached_lut is not None and _cached_lut.key == key and _cached_lut.path.is_file():
            return _cached_lut
    directory = tempfile.TemporaryDirectory(prefix="visual-enhancer-preview-lut-")
    try:
        path = Path(directory.name) / "filter.cube"
        base = load_cube_lut(settings.lut_path) if settings.lut_path else identity_cube_lut()
        lut = compose_cube_lut(base, settings, settings.lut_resolution, controller=controller)
        write_composed_lut(path, lut, controller=controller)
        if controller is not None and controller.cancel.is_set():
            raise Cancelled("Preview superseded by newer settings.")
        prepared = _CachedLut(key, directory, path)
        with _lock:
            _cached_lut = prepared
        # A caller keeps the entry alive until its GPU command finishes, even
        # if another render replaces the cache. TemporaryDirectory then cleans
        # up the old cube once its last user releases it.
        return prepared
    except BaseException:
        directory.cleanup()
        raise


class FastPreviewUnavailable(ValueError):
    """Use the file-backed preview when this source cannot use the RAM path."""


def _source_key(source: str | Path, mode: str, start_seconds: float,
                size: tuple[int, int] | None) -> tuple:
    path = Path(source).resolve()
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size, mode,
            round(float(start_seconds), 6) if mode == "Video" else 0,
            size if mode == "Video" else None)


def _video_frame(path: Path, start_seconds: float, size: tuple[int, int], controller=None) -> np.ndarray:
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
    command = prepare_command(command)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if controller is not None:
        controller.register(process)
    try:
        pixels, errors = process.communicate(timeout=30)
        if controller is not None and controller.cancel.is_set():
            raise Cancelled("Preview superseded by newer settings.")
        if process.returncode != 0:
            raise RuntimeError(errors.decode("utf-8", "replace").strip()
                               or "Could not decode the preview frame.")
        if len(pixels) != width * height * 8:
            raise FastPreviewUnavailable("Decoded frame dimensions differ from the source metadata.")
        return np.frombuffer(pixels, dtype="<u2").reshape(height, width, 4)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        if controller is not None:
            controller.unregister(process)


def _frame(key: tuple, size: tuple[int, int] | None, controller=None) -> _CachedFrame:
    global _cached_frame
    with _lock:
        if _cached_frame is not None and _cached_frame.key == key:
            return _cached_frame
    path = Path(key[0])
    rgba = decode_image(path).rgba if key[3] == "Image" else _video_frame(path, key[4], size, controller)
    if rgba.shape[0] * rgba.shape[1] > MAX_PREVIEW_PIXELS:
        raise FastPreviewUnavailable("This image is too large for the in-memory preview.")
    frame = _CachedFrame(key, rgba)
    if rgba.nbytes <= _MAX_CACHE_BYTES:
        with _lock:
            _cached_frame = frame
    return frame


def render_color_still_preview(source: str | Path, settings: UISettings, mode: str,
                               *, start_seconds: float = 0.0,
                               video_size: tuple[int, int] | None = None,
                               controller=None, progress=None) -> tuple[np.ndarray, str]:
    """Grade a source-resolution frame on Vulkan with the same cube as export."""
    if mode not in {"Image", "Video"}:
        raise ValueError("Unsupported coloring preview mode.")
    if mode == "Video" and video_size is None:
        raise ValueError("Video dimensions are required for the fast preview.")
    key = _source_key(source, mode, start_seconds, video_size)
    if controller is not None and controller.cancel.is_set():
        raise Cancelled("Preview superseded by newer settings.")
    frame = _frame(key, video_size, controller)
    if progress is not None:
        progress(0.35, "Coloring: source frame ready")
    if controller is not None and controller.cancel.is_set():
        raise Cancelled("Preview superseded by newer settings.")
    prepared = _prepared_lut(settings, controller)
    result = apply_cube_lut_file(frame.rgba, prepared.path, controller=controller, selection=settings.ffmpeg_device)
    if controller is not None and controller.cancel.is_set():
        raise Cancelled("Preview superseded by newer settings.")
    if progress is not None:
        progress(1.0, "Coloring preview complete")
    return result, ""
