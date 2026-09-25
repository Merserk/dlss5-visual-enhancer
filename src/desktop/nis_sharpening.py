"""NVIDIA Image Scaling SDK 1.0.3 NVSharpen-only math for RGB media.

This is a vectorized CPU port of the ``NIS_SCALER=0`` path in
``native (dev)/NVIDIAImageScaling/NIS/NIS_Scaler.h``. It uses the SDK's
``NVSharpenUpdateConfig`` parameters, four directional USM filters,
edge detection and anti-ringing limiter. No NIS scaling code is used.
"""

# NVSharpen algorithm adapted from NVIDIA Image Scaling SDK 1.0.3.
# Copyright (c) 2022 NVIDIA CORPORATION & AFFILIATES.
# See NVIDIA-NIS-LICENSE.txt in this directory.

from __future__ import annotations

import numpy as np

from ..core.jobs import Cancelled, JobController


def _configuration(sharpness: int) -> tuple[float, float, float, float, float, float]:
    """The SDR constants from NVScalerUpdateConfig / NVSharpenUpdateConfig."""
    slider = sharpness / 100.0 - 0.5
    max_scale = 1.25 if slider >= 0.0 else 1.75
    min_scale = 1.25 if slider >= 0.0 else 1.0
    limit_scale = 1.25 if slider >= 0.0 else 1.0
    strength_min = max(0.0, 0.4 + slider * min_scale * 1.2)
    strength_max = 1.6 + slider * max_scale * 1.8
    limit_min = max(0.1, 0.14 + slider * limit_scale * 0.32)
    limit_max = 0.5 + slider * limit_scale * 0.6
    return (0.45, 1.0 / (0.9 - 0.45), strength_min,
            strength_max - strength_min, limit_min, limit_max - limit_min)


def _lti(samples: tuple[np.ndarray, ...]) -> np.ndarray:
    a, b, c, d, e = samples
    a_min = np.minimum(np.minimum(a, b), c)
    a_max = np.maximum(np.maximum(a, b), c)
    b_min = np.minimum(np.minimum(c, d), e)
    b_max = np.maximum(np.maximum(c, d), e)
    a_contrast = a_max - a_min
    b_contrast = b_max - b_min
    ratio = np.maximum(a_contrast, b_contrast) / (
        np.minimum(a_contrast, b_contrast) + np.float32(1.0 / 255.0))
    return 1.0 - np.clip((ratio - 2.0) / 8.0, 0.0, 1.0)


def _directional_usm(samples: tuple[np.ndarray, ...], strength: np.ndarray,
                     limit: np.ndarray) -> np.ndarray:
    usm = (-0.6001 * samples[1] + 1.2002 * samples[2]
           - 0.6001 * samples[3])
    usm = np.clip(usm * strength, -limit, limit)
    return usm * _lti(samples)


def _edge_weights(p: list[list[np.ndarray]]) -> tuple[np.ndarray, ...]:
    """GetEdgeMap(p, 1, 1) from the SDK's NVSharpen path."""
    g0 = np.abs(p[1][1] + p[1][2] + p[1][3]
                - p[3][1] - p[3][2] - p[3][3])
    g45 = np.abs(p[2][1] + p[1][1] + p[1][2]
                 - p[3][2] - p[3][3] - p[2][3])
    g90 = np.abs(p[1][1] + p[2][1] + p[3][1]
                 - p[1][3] - p[2][3] - p[3][3])
    g135 = np.abs(p[2][1] + p[3][1] + p[3][2]
                  - p[1][2] - p[1][3] - p[2][3])

    axis_max = np.maximum(g0, g90)
    axis_min = np.minimum(g0, g90)
    diagonal_max = np.maximum(g45, g135)
    diagonal_min = np.minimum(g45, g135)
    total = axis_max + diagonal_max
    axis_fraction = np.divide(axis_max, total, out=np.zeros_like(total),
                              where=total > 0)
    diagonal_fraction = 1.0 - axis_fraction
    detect_ratio = 2.0 * 1127.0 / 1024.0
    threshold = 64.0 / 1024.0
    axis_detected = ((axis_max > axis_min * detect_ratio) &
                     (axis_max > threshold) & (axis_max > diagonal_min))
    diagonal_detected = ((diagonal_max > diagonal_min * detect_ratio) &
                         (diagonal_max > threshold) & (diagonal_max > axis_min))
    both = axis_detected & diagonal_detected
    axis_factor = np.where(both, axis_fraction, 1.0)
    diagonal_factor = np.where(both, diagonal_fraction, 1.0)
    return (np.where(axis_detected & (g0 >= g90), axis_factor, 0.0),
            np.where(axis_detected & (g0 < g90), axis_factor, 0.0),
            np.where(diagonal_detected & (g45 >= g135), diagonal_factor, 0.0),
            np.where(diagonal_detected & (g45 < g135), diagonal_factor, 0.0))


def sharpen_rgb(pixels: np.ndarray, sharpness: int, *,
                alpha: np.ndarray | None = None,
                controller: JobController | None = None) -> np.ndarray:
    """Apply SDK NVSharpen without resizing 8/16-bit display-referred RGB."""
    if pixels.ndim != 3 or pixels.shape[2] != 3 or pixels.dtype not in (np.uint8, np.uint16):
        raise ValueError("NIS requires an 8-bit or 16-bit RGB image.")
    if isinstance(sharpness, bool) or not isinstance(sharpness, int) or not 0 <= sharpness <= 100:
        raise ValueError("NIS sharpness must be an integer from 0 to 100.")
    if alpha is not None and (alpha.shape != pixels.shape[:2] or alpha.dtype != pixels.dtype):
        raise ValueError("NIS alpha must match the RGB pixels.")
    if sharpness == 0 or min(pixels.shape[:2]) < 2:
        return pixels.copy()

    height, width = pixels.shape[:2]
    maximum = float(np.iinfo(pixels.dtype).max)
    start_y, scale_y, strength_min, strength_scale, limit_min, limit_scale = _configuration(sharpness)
    result = np.empty_like(pixels)
    tile = 256
    for y in range(0, height, tile):
        for x in range(0, width, tile):
            if controller is not None and controller.cancel.is_set():
                raise Cancelled("Render stopped by user.")
            bottom, right = min(height, y + tile), min(width, x + tile)
            tile_height, tile_width = bottom - y, right - x
            y0, y1 = max(0, y - 2), min(height, bottom + 2)
            x0, x1 = max(0, x - 2), min(width, right + 2)
            border = ((max(0, 2 - y), max(0, bottom + 2 - height)),
                      (max(0, 2 - x), max(0, right + 2 - width)))
            block = np.pad(pixels[y0:y1, x0:x1], (*border, (0, 0)), mode="edge")
            block = block.astype(np.float32) / maximum
            luma = (np.float32(0.2126) * block[..., 0]
                    + np.float32(0.7152) * block[..., 1]
                    + np.float32(0.0722) * block[..., 2])
            p = [[luma[i:i + tile_height, j:j + tile_width]
                  for j in range(5)] for i in range(5)]

            scale = 1.0 - np.clip((p[2][2] - start_y) * scale_y, 0.0, 1.0)
            strength = scale * strength_scale + strength_min
            limit = (scale * limit_scale + limit_min) * p[2][2]
            directions = (
                (p[0][2], p[1][2], p[2][2], p[3][2], p[4][2]),
                (p[2][0], p[2][1], p[2][2], p[2][3], p[2][4]),
                (p[1][1], 0.5 * (p[2][1] + p[1][2]), p[2][2],
                 0.5 * (p[3][2] + p[2][3]), p[3][3]),
                (p[3][1], 0.5 * (p[3][2] + p[2][1]), p[2][2],
                 0.5 * (p[2][3] + p[1][2]), p[1][3]),
            )
            weights = _edge_weights(p)
            usm = np.zeros_like(p[2][2])
            for samples, weight in zip(directions, weights):
                usm += _directional_usm(samples, strength, limit) * weight

            center = block[2:-2, 2:-2]
            if alpha is not None:
                a = np.pad(alpha[y0:y1, x0:x1], border, mode="edge")
                opaque = np.ones((tile_height, tile_width), dtype=bool)
                for dy in range(5):
                    for dx in range(5):
                        opaque &= a[dy:dy + tile_height, dx:dx + tile_width] == maximum
                usm = np.where(opaque, usm, 0.0)
            sharpened = np.clip(center + usm[..., None], 0.0, 1.0)
            result[y:bottom, x:right] = np.rint(sharpened * maximum).astype(pixels.dtype)
    return result
