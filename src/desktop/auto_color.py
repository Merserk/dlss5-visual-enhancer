"""Content-aware starting point for editable LUT color adjustments.

Auto analyzes pixels at the source's native precision.  Only the statistics
use a bounded sample; the chosen settings are applied to full-resolution
previews, exports, and saved cubes by the existing grading pipeline.
"""

from __future__ import annotations

import math
import subprocess
from dataclasses import replace
from pathlib import Path

import numpy as np

from ..core.jobs import Cancelled
from ..core.paths import FFMPEG
from ..neural_rendering.image.decoder import decode_image
from ..settings.models import DEFAULT_SETTINGS, LUT_ADJUSTMENT_RANGES, UISettings
from .coloring import grade_lut_rgb, load_cube_lut, sample_cube_lut


_LUMA = np.asarray((0.2126, 0.7152, 0.0722), dtype=np.float32)


def _check_cancel(controller) -> None:
    if controller is not None and controller.cancel.is_set():
        raise Cancelled("Automatic color adjustment cancelled.")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _analysis_pixels(rgba: np.ndarray, limit: int) -> np.ndarray:
    """Sample a spatial grid while discarding transparent pixels."""
    height, width = rgba.shape[:2]
    stride = max(1, math.ceil(max(height, width) / 512))
    sampled = rgba[::stride, ::stride].reshape(-1, rgba.shape[-1])
    if len(sampled) > limit:
        sampled = sampled[np.linspace(0, len(sampled) - 1, limit, dtype=np.intp)]
    maximum = float(np.iinfo(rgba.dtype).max)
    if sampled.shape[-1] == 4:
        sampled = sampled[sampled[:, 3] > maximum * 0.05]
    return np.ascontiguousarray(sampled[:, :3].astype(np.float32) / maximum)


def _video_sample(source: Path, seconds: float, size: tuple[int, int], controller) -> np.ndarray | None:
    width, height = size
    scale = min(1.0, 512.0 / max(width, height))
    sample_width = max(1, round(width * scale))
    sample_height = max(1, round(height * scale))
    command = [str(FFMPEG), "-hide_banner", "-loglevel", "error",
               "-ss", f"{max(0.0, seconds):.6f}", "-i", str(source),
               "-frames:v", "1", "-an", "-vf",
               f"scale={sample_width}:{sample_height}:flags=area",
               "-f", "rawvideo", "-pix_fmt", "rgb48le", "pipe:1"]
    _check_cancel(controller)
    result = subprocess.run(
        command, capture_output=True, timeout=30, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _check_cancel(controller)
    if result.returncode != 0 or len(result.stdout) != sample_width * sample_height * 6:
        return None
    rgb = np.frombuffer(result.stdout, dtype="<u2").reshape(sample_height, sample_width, 3)
    return _analysis_pixels(rgb, 32_000)


def _source_pixels(source: Path, mode: str, duration_seconds: float,
                   video_size: tuple[int, int] | None, controller, progress) -> np.ndarray:
    if mode == "Image":
        _check_cancel(controller)
        pixels = _analysis_pixels(decode_image(source).rgba, 220_000)
        _check_cancel(controller)
    elif mode == "Video":
        if video_size is None or min(video_size) < 1:
            raise ValueError("Video dimensions are unavailable; reload the input and try Auto again.")
        if duration_seconds > 0:
            stamps = [duration_seconds * fraction for fraction in
                      (0.05, 0.2, 0.35, 0.5, 0.65, 0.8, 0.95)]
        else:
            stamps = [0.0, 1.0, 3.0, 6.0]
        frames = []
        fallback_frames = []
        for index, stamp in enumerate(stamps):
            sample = _video_sample(source, stamp, video_size, controller)
            if sample is not None and len(sample):
                fallback_frames.append(sample)
                luma = sample @ _LUMA
                # Ignore nearly black/white transition frames when there are
                # other usable frames in the sequence.
                if 0.025 < float(np.median(luma)) < 0.975:
                    frames.append(sample)
            if progress is not None:
                progress((index + 1) / (len(stamps) + 1), "Analyzing video color")
        if not frames:
            frames = fallback_frames
        if not frames:
            raise ValueError("Could not find a usable video frame for Auto color adjustment.")
        pixels = np.concatenate(frames, axis=0)
    else:
        raise ValueError("Auto color adjustment requires an image or video input.")
    if len(pixels) < 32:
        raise ValueError("The input has too few visible pixels for Auto color adjustment.")
    return pixels


def _linear(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def recommend_lut_adjustments(identity: np.ndarray, sampled: np.ndarray,
                              *, allow_white_balance: bool = True,
                              hdr: bool = False) -> dict[str, int | float]:
    """Choose a restrained grade from robust tone and neutral-color statistics.

    A universal "best" look cannot be inferred from pixels alone, so Auto
    favors highlight protection, modest contrast, and minimal color shifts.
    All returned controls remain editable by the user.
    """
    if identity.shape != sampled.shape or identity.ndim != 2 or identity.shape[1] != 3:
        raise ValueError("Auto color analysis requires matching RGB samples.")
    valid = np.all(np.isfinite(identity) & np.isfinite(sampled), axis=1)
    identity = np.clip(identity[valid], 0.0, 1.0)
    sampled = np.clip(sampled[valid], 0.0, 1.0)
    if len(identity) < 32:
        raise ValueError("The input has too few valid colors for Auto adjustment.")

    luma = sampled @ _LUMA
    p02, p10, p50, p90, p95, p98, p99 = np.percentile(
        luma, (2, 10, 50, 90, 95, 98, 99))
    if p98 - p02 < 0.06:
        # A blank/faded frame gives no reliable evidence of the intended look.
        return {key: getattr(DEFAULT_SETTINGS, key) for key in LUT_ADJUSTMENT_RANGES}
    exposure = 0.55 * math.log2(_linear(0.82) / max(_linear(float(p95)), 0.003))
    exposure = _clamp(exposure, -0.7, 1.2)
    if p99 > 0.75:
        highlight_limit = 0.8 * math.log2(_linear(0.98) / max(_linear(float(p99)), 0.003))
        exposure = min(exposure, highlight_limit)
    if p50 < 0.22 and p99 > 0.7:
        exposure = min(exposure, 0.25)  # Preserve low-key scenes with bright accents.
    if p50 > 0.72 and p02 > 0.2:
        exposure = max(exposure, -0.25)  # Preserve a deliberate high-key scene.
    if hdr:
        exposure = _clamp(exposure, -0.25, 0.25)
    exposure = round(round(_clamp(exposure, -3.0, 3.0) / 0.05) * 0.05, 2)

    base_settings = replace(DEFAULT_SETTINGS, lut_exposure=exposure)
    exposed = grade_lut_rgb(sampled, sampled, base_settings)
    exposed_luma = exposed @ _LUMA
    e02, e10, e50, e90, e95, e98, e99 = np.percentile(
        exposed_luma, (2, 10, 50, 90, 95, 98, 99))
    contrast = round(_clamp((0.64 / max(float(e90 - e10), 0.15) - 1.0) * 20.0, -12, 20))
    shadows = round(_clamp((0.13 - float(e10)) * 115, 0, 18))
    highlights = -round(_clamp((float(e99) - 0.92) * 140, 0, 25))
    whites = round(_clamp((0.86 - float(e98)) * 60, 0, 12))
    blacks = -round(_clamp((float(e02) - 0.05) * 130, 0, 18))
    midtones = round(_clamp((0.30 - float(e50)) * 45, 0, 10)) if e99 > 0.7 else 0

    temperature = tint = 0
    if allow_white_balance:
        chroma = np.ptp(sampled, axis=1)
        neutral = (chroma < 0.055) & (luma > 0.18) & (luma < 0.88)
        if np.count_nonzero(neutral) >= max(100, int(len(sampled) * 0.02)):
            r, g, b = np.median(sampled[neutral], axis=0)
            level = max(float((r + g + b) / 3), 0.1)
            temperature = round(_clamp(float(b - r) / level * 125, -16, 16))
            tint = round(_clamp(float(g - (r + b) * 0.5) / level * 220, -12, 12))

    chroma = np.ptp(sampled, axis=1)
    medium = chroma[(luma > 0.12) & (luma < 0.9)]
    mean_chroma = float(np.mean(medium)) if len(medium) else 0.0
    vibrance = (round(_clamp((0.2 - mean_chroma) * 75, 0, 16))
                if mean_chroma > 0.035 else 0)
    saturation = -round(_clamp((mean_chroma - 0.4) * 30, 0, 8))

    values: dict[str, int | float] = {key: getattr(DEFAULT_SETTINGS, key)
                                      for key in LUT_ADJUSTMENT_RANGES}
    values.update(lut_mix=100, lut_exposure=exposure, lut_contrast=contrast,
                  lut_highlights=highlights, lut_shadows=shadows,
                  lut_whites=whites, lut_blacks=blacks, lut_midtones=midtones,
                  lut_temperature=temperature, lut_tint=tint,
                  lut_vibrance=vibrance, lut_saturation=saturation)

    # Reject a newly clipped look.  Existing clipped highlights cannot be
    # recovered, but Auto must not substantially add to them.
    baseline_white = float(np.mean(np.any(sampled >= 0.997, axis=1)))
    baseline_black = float(np.mean(np.all(sampled <= 0.003, axis=1)))
    for _ in range(8):
        graded = grade_lut_rgb(identity, sampled, replace(DEFAULT_SETTINGS, **values))
        white = float(np.mean(np.any(graded >= 0.997, axis=1)))
        black = float(np.mean(np.all(graded <= 0.003, axis=1)))
        if white <= baseline_white + 0.012 and black <= baseline_black + 0.012:
            break
        if white > baseline_white + 0.012:
            if values["lut_whites"] > 0:
                values["lut_whites"] = max(0, int(values["lut_whites"]) - 3)
            elif values["lut_exposure"] > 0:
                values["lut_exposure"] = round(max(0.0, float(values["lut_exposure"]) - 0.05), 2)
            else:
                values["lut_highlights"] = max(-35, int(values["lut_highlights"]) - 4)
        if black > baseline_black + 0.012:
            values["lut_blacks"] = min(0, int(values["lut_blacks"]) + 3)
            values["lut_contrast"] = max(-12, int(values["lut_contrast"]) - 2)
    return values


def analyze_color_source(source: str | Path, settings: UISettings, mode: str, *,
                         duration_seconds: float = 0.0,
                         video_size: tuple[int, int] | None = None,
                         hdr: bool = False, controller=None, progress=None) -> dict[str, int | float]:
    """Analyze the selected source and return editable Color Adjustment values."""
    path = Path(source).resolve()
    pixels = _source_pixels(path, mode, duration_seconds, video_size, controller, progress)
    _check_cancel(controller)
    if settings.lut_path:
        lut = load_cube_lut(settings.lut_path)
        sampled = np.empty_like(pixels)
        for offset in range(0, len(pixels), 65_536):
            _check_cancel(controller)
            end = min(len(pixels), offset + 65_536)
            sampled[offset:end] = sample_cube_lut(pixels[offset:end], lut)
    else:
        sampled = pixels
    values = recommend_lut_adjustments(pixels, sampled,
                                       allow_white_balance=not (settings.lut_path or hdr), hdr=hdr)
    if progress is not None:
        progress(1.0, "Auto color adjustment ready")
    return values
