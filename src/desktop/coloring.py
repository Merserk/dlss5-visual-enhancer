"""Perceptual color matching and 3D .cube LUT grading for images and video."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from ..settings.models import LUT_ADJUSTMENT_RANGES, UISettings


@dataclass(frozen=True)
class CubeLUT:
    size: int
    domain_min: np.ndarray
    domain_max: np.ndarray
    # .cube entries are stored with red changing fastest, then green, then blue.
    values: np.ndarray  # [blue, green, red, output channel]


@lru_cache(maxsize=1)
def identity_cube_lut() -> CubeLUT:
    """Use the original colors as the base when no .cube file is selected."""
    levels = np.asarray((0.0, 1.0), dtype=np.float32)
    blue, green, red = np.meshgrid(levels, levels, levels, indexing="ij")
    values = np.stack((red, green, blue), axis=-1)
    return CubeLUT(2, np.zeros(3, dtype=np.float32),
                   np.ones(3, dtype=np.float32), values)


def load_cube_lut(file_path: str | Path) -> CubeLUT:
    """Read a 3D .cube file, caching by path and modification time."""
    path = Path(file_path).resolve()
    if path.suffix.casefold() != ".cube":
        raise ValueError("Choose a .cube LUT file.")
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"LUT file is unavailable: {path}") from exc
    if not path.is_file() or stat.st_size > 128 * 1024 * 1024:
        raise ValueError("LUT must be an existing .cube file smaller than 128 MiB.")
    return _load_cube_cached(str(path), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=4)
def _load_cube_cached(path: str, mtime_ns: int, file_size: int) -> CubeLUT:
    del mtime_ns, file_size  # Cache keys make an edited LUT reload on the next render.
    size: int | None = None
    domain_min = np.zeros(3, dtype=np.float32)
    domain_max = np.ones(3, dtype=np.float32)
    entries: np.ndarray | None = None
    row_count = 0
    try:
        with Path(path).open("r", encoding="utf-8-sig") as stream:
            for line_number, raw in enumerate(stream, 1):
                line = raw.partition("#")[0].strip()
                if not line:
                    continue
                words = line.split()
                key = words[0].upper()
                if key == "TITLE":
                    continue
                if key == "LUT_3D_SIZE":
                    if size is not None or len(words) != 2:
                        raise ValueError("Invalid LUT_3D_SIZE declaration.")
                    size = int(words[1])
                    if not 2 <= size <= 129:
                        raise ValueError("3D LUT size must be between 2 and 129.")
                    entries = np.empty((size ** 3, 3), dtype=np.float32)
                    continue
                if key == "LUT_1D_SIZE":
                    raise ValueError("Select a 3D .cube LUT; 1D LUTs are not supported.")
                if key in {"DOMAIN_MIN", "DOMAIN_MAX"}:
                    if len(words) != 4:
                        raise ValueError(f"Invalid {key} declaration.")
                    values = np.asarray([float(value) for value in words[1:]], dtype=np.float32)
                    if not np.all(np.isfinite(values)):
                        raise ValueError(f"Invalid {key} values.")
                    if key == "DOMAIN_MIN":
                        domain_min = values
                    else:
                        domain_max = values
                    continue
                if key == "LUT_3D_INPUT_RANGE":
                    if len(words) != 3:
                        raise ValueError("Invalid LUT_3D_INPUT_RANGE declaration.")
                    domain_min = np.full(3, float(words[1]), dtype=np.float32)
                    domain_max = np.full(3, float(words[2]), dtype=np.float32)
                    continue
                if size is None or len(words) != 3:
                    raise ValueError(f"Invalid 3D LUT entry on line {line_number}.")
                values = tuple(float(value) for value in words)
                if not all(np.isfinite(value) for value in values):
                    raise ValueError(f"Nonfinite 3D LUT entry on line {line_number}.")
                if row_count >= size ** 3:
                    raise ValueError("The 3D LUT contains more entries than declared.")
                assert entries is not None
                entries[row_count] = values
                row_count += 1
    except (OSError, UnicodeError, OverflowError) as exc:
        raise ValueError(f"Cannot read .cube LUT: {path}") from exc
    if size is None or row_count != size ** 3:
        raise ValueError("The 3D LUT has an incorrect number of color entries.")
    if np.any(domain_max <= domain_min):
        raise ValueError("3D LUT DOMAIN_MAX must exceed DOMAIN_MIN on every channel.")
    assert entries is not None
    values = entries.reshape(size, size, size, 3)
    if not np.all(np.isfinite(domain_min)) or not np.all(np.isfinite(domain_max)) or not np.all(np.isfinite(values)):
        raise ValueError("3D LUT contains values outside the supported numeric range.")
    return CubeLUT(size, domain_min, domain_max, values)


def sample_cube_lut(rgb: np.ndarray, lut: CubeLUT) -> np.ndarray:
    """Sample a 3D LUT at floating point RGB coordinates without quantization."""
    coords = np.clip((rgb - lut.domain_min) / (lut.domain_max - lut.domain_min), 0.0, 1.0) * (lut.size - 1)
    low = np.floor(coords).astype(np.intp)
    high = np.minimum(low + 1, lut.size - 1)
    fraction = coords - low
    r0, g0, b0 = (low[..., channel] for channel in range(3))
    r1, g1, b1 = (high[..., channel] for channel in range(3))
    fr, fg, fb = (fraction[..., channel, None] for channel in range(3))
    table = lut.values
    c00 = table[b0, g0, r0] * (1 - fr) + table[b0, g0, r1] * fr
    c01 = table[b0, g1, r0] * (1 - fr) + table[b0, g1, r1] * fr
    c10 = table[b1, g0, r0] * (1 - fr) + table[b1, g0, r1] * fr
    c11 = table[b1, g1, r0] * (1 - fr) + table[b1, g1, r1] * fr
    return (c00 * (1 - fg) + c01 * fg) * (1 - fb) + (c10 * (1 - fg) + c11 * fg) * fb


def has_lut_adjustments(settings: UISettings) -> bool:
    return any(getattr(settings, key) != (100 if key == "lut_mix" else 0)
               for key in LUT_ADJUSTMENT_RANGES)


def _smoothstep(low: float, high: float, value: np.ndarray) -> np.ndarray:
    t = np.clip((value - low) / (high - low), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def grade_lut_rgb(identity: np.ndarray, sampled: np.ndarray, settings: UISettings) -> np.ndarray:
    """Grade display-referred LUT output and blend it with the input RGB."""
    mix = settings.lut_mix / 100.0
    if mix == 0:
        return np.clip(identity, 0.0, 1.0)
    if not has_lut_adjustments(settings):
        return sampled

    rgb = np.clip(sampled, 0.0, 1.0).astype(np.float32, copy=True)
    if settings.lut_exposure or settings.lut_temperature or settings.lut_tint:
        linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
        warmth = settings.lut_temperature / 100.0
        tint = settings.lut_tint / 100.0
        gains = np.asarray((1.0 + 0.2 * warmth + 0.075 * tint,
                            1.0 - 0.15 * tint,
                            1.0 - 0.2 * warmth + 0.075 * tint), dtype=np.float32)
        linear = np.maximum(linear * gains * (2.0 ** settings.lut_exposure), 0.0)
        rgb = np.where(linear <= 0.0031308, linear * 12.92,
                       1.055 * np.power(linear, 1.0 / 2.4) - 0.055)
        rgb = np.clip(rgb, 0.0, 1.0)

    if settings.lut_contrast:
        rgb = np.clip((rgb - 0.5) * (1.0 + settings.lut_contrast / 100.0) + 0.5, 0.0, 1.0)
    if any((settings.lut_shadows, settings.lut_highlights, settings.lut_whites, settings.lut_blacks)):
        luma = np.dot(rgb, np.asarray((0.2126, 0.7152, 0.0722), dtype=np.float32))
        shift = (settings.lut_shadows / 100.0 * 0.32 * (1.0 - luma) ** 2
                 + settings.lut_highlights / 100.0 * 0.32 * luma ** 2
                 + settings.lut_whites / 100.0 * 0.18 * _smoothstep(0.62, 1.0, luma)
                 + settings.lut_blacks / 100.0 * 0.18 * (1.0 - _smoothstep(0.0, 0.38, luma)))
        rgb = np.clip(rgb + shift[..., None], 0.0, 1.0)
    if settings.lut_midtones:
        rgb = np.power(rgb, 2.0 ** (-settings.lut_midtones / 100.0))
    if settings.lut_hue:
        # Rotate chroma in YIQ while keeping its luminance component.
        angle = np.deg2rad(settings.lut_hue)
        cosine, sine = np.cos(angle), np.sin(angle)
        y = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
        i = rgb[..., 0] * 0.596 - rgb[..., 1] * 0.274 - rgb[..., 2] * 0.322
        q = rgb[..., 0] * 0.211 - rgb[..., 1] * 0.523 + rgb[..., 2] * 0.312
        rotated_i = i * cosine - q * sine
        rotated_q = i * sine + q * cosine
        rgb = np.stack((y + 0.956 * rotated_i + 0.621 * rotated_q,
                        y - 0.272 * rotated_i - 0.647 * rotated_q,
                        y - 1.106 * rotated_i + 1.703 * rotated_q), axis=-1)
        rgb = np.clip(rgb, 0.0, 1.0)
    if settings.lut_saturation or settings.lut_vibrance:
        luma = np.dot(rgb, np.asarray((0.2126, 0.7152, 0.0722), dtype=np.float32))[..., None]
        chroma = np.max(rgb, axis=-1, keepdims=True) - np.min(rgb, axis=-1, keepdims=True)
        factor = (1.0 + settings.lut_saturation / 100.0
                  + settings.lut_vibrance / 100.0 * (1.0 - chroma) ** 2)
        rgb = np.clip(luma + (rgb - luma) * np.maximum(0.0, factor), 0.0, 1.0)
    return np.clip(identity * (1.0 - mix) + rgb * mix, 0.0, 1.0)


def _graded_cube_slices(source: CubeLUT, settings: UISettings, size: int):
    levels = np.linspace(0.0, 1.0, size, dtype=np.float32)
    red = np.tile(levels, size)
    green = np.repeat(levels, size)
    for blue in levels:
        identity = np.column_stack((red, green, np.full_like(red, blue)))
        sampled = sample_cube_lut(identity, source)
        yield grade_lut_rgb(identity, sampled, settings).reshape(size, size, 3)


def compose_cube_lut(source: CubeLUT, settings: UISettings, size: int) -> CubeLUT:
    """Bake the selected LUT and grading controls into a standard RGB cube."""
    if size not in (33, 65, 129):
        raise ValueError("LUT resolution must be 33, 65, or 129.")
    values = np.empty((size, size, size, 3), dtype=np.float32)
    for index, plane in enumerate(_graded_cube_slices(source, settings, size)):
        values[index] = plane
    return CubeLUT(size, np.zeros(3, dtype=np.float32),
                   np.ones(3, dtype=np.float32), values)


def save_cube_lut(destination: str | Path, source_path: str | Path | None,
                  settings: UISettings, *, controller=None, progress=None) -> Path:
    """Write a baked .cube atomically, keeping memory bounded at 129³."""
    source = load_cube_lut(source_path) if source_path else identity_cube_lut()
    size = settings.lut_resolution
    if size not in (33, 65, 129):
        raise ValueError("LUT resolution must be 33, 65, or 129.")
    target = Path(destination).expanduser().resolve()
    if target.suffix.casefold() != ".cube":
        target = target.with_suffix(".cube")
    if not target.parent.is_dir():
        raise ValueError("Choose an existing folder for the saved LUT.")
    fd, temporary = tempfile.mkstemp(prefix=target.stem + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as stream:
            stream.write('TITLE "Visual Enhancer Graded LUT"\n')
            stream.write(f"LUT_3D_SIZE {size}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n")
            for index, plane in enumerate(_graded_cube_slices(source, settings, size)):
                if controller is not None and controller.cancel.is_set():
                    from ..core.jobs import Cancelled
                    raise Cancelled("LUT export cancelled.")
                np.savetxt(stream, plane.reshape(-1, 3), fmt="%.7f")
                if progress is not None:
                    progress((index + 1) / size, "Saving LUT")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def apply_cube_lut(rgba: np.ndarray, lut: CubeLUT) -> np.ndarray:
    """Apply trilinear RGB interpolation in chunks, preserving source alpha and depth."""
    result = np.empty_like(rgba)
    result[..., 3] = rgba[..., 3]
    maximum = float(np.iinfo(rgba.dtype).max)
    rows_per_chunk = max(1, 262_144 // max(1, rgba.shape[1]))
    for top in range(0, rgba.shape[0], rows_per_chunk):
        bottom = min(rgba.shape[0], top + rows_per_chunk)
        rgb = rgba[top:bottom, :, :3].astype(np.float32) / maximum
        color = sample_cube_lut(rgb, lut)
        result[top:bottom, :, :3] = np.rint(np.clip(color, 0.0, 1.0) * maximum).astype(rgba.dtype)
    return result


def _lab_statistics(rgba: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """Measure visible pixels on a bounded preview, regardless of image size."""
    height, width = rgba.shape[:2]
    scale = min(1.0, 512.0 / max(height, width))
    if scale < 1.0:
        sample = cv2.resize(rgba, (max(1, round(width * scale)),
                                   max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
    else:
        sample = rgba
    maximum = float(np.iinfo(sample.dtype).max)
    visible = sample[..., 3] > maximum * 0.05
    if not np.any(visible):
        return None
    rgb = sample[..., :3].astype(np.float32) / maximum
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2Lab)
    pixels = lab[visible]
    # Trim sparse highlights and deep shadows so isolated pixels do not
    # determine the grade of the entire image.
    lower, upper = np.percentile(pixels, (2, 98), axis=0)
    pixels = np.clip(pixels, lower, upper)
    return pixels.mean(axis=0), pixels.std(axis=0)


def match_image_colors(rgba: np.ndarray, reference_rgba: np.ndarray) -> np.ndarray:
    """Match Lab channel distributions while keeping detail and alpha intact.

    The transform is measured from each image independently, then applied in
    row chunks so large and high bit depth images do not need a full size Lab
    working copy. The bounded contrast ratio avoids destructive flat areas.
    """
    source_stats = _lab_statistics(rgba)
    reference_stats = _lab_statistics(reference_rgba)
    if source_stats is None or reference_stats is None:
        return rgba.copy()
    source_mean, source_std = source_stats
    target_mean, target_std = reference_stats
    ratio = np.clip(target_std / np.maximum(source_std, 1.0), 0.5, 2.0)
    result = np.empty_like(rgba)
    result[..., 3] = rgba[..., 3]
    maximum = float(np.iinfo(rgba.dtype).max)
    rows = max(1, 1_000_000 // max(1, rgba.shape[1]))
    for top in range(0, rgba.shape[0], rows):
        bottom = min(rgba.shape[0], top + rows)
        rgb = rgba[top:bottom, :, :3].astype(np.float32) / maximum
        lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2Lab)
        lab = (lab - source_mean) * ratio + target_mean
        lab[..., 0] = np.clip(lab[..., 0], 0.0, 100.0)
        lab[..., 1:] = np.clip(lab[..., 1:], -127.0, 127.0)
        matched = cv2.cvtColor(lab.astype(np.float32), cv2.COLOR_Lab2RGB)
        result[top:bottom, :, :3] = np.rint(np.clip(matched, 0.0, 1.0) * maximum).astype(rgba.dtype)
    return result
