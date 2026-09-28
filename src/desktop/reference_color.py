"""Fit editable grading controls to an image's perceptual color distribution.

Only bounded floating-point samples are analyzed here. Full-resolution
rendering uses the existing Vulkan LUT pipeline; no image-specific pixel
transform is hidden outside the visible sliders or exported cube.
"""
from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import numpy as np

from ..neural_rendering.image.decoder import decode_image
from ..settings.models import LUT_ADJUSTMENT_RANGES, UISettings
from .auto_color import _analysis_pixels, _check_cancel, _source_pixels
from .coloring import grade_lut_rgb, load_cube_lut, sample_cube_lut


_KEYS = tuple(key for key in LUT_ADJUSTMENT_RANGES if key != "lut_mix")
_SCALES = np.asarray([1.0 if key == "lut_exposure" else
                      90.0 if key == "lut_hue" else 100.0 for key in _KEYS])
_LOW = np.asarray([LUT_ADJUSTMENT_RANGES[key][0] for key in _KEYS]) / _SCALES
_HIGH = np.asarray([LUT_ADJUSTMENT_RANGES[key][1] for key in _KEYS]) / _SCALES
_QUANTILES = np.asarray((.005, .01, .025, .05, .10, .15, .20, .25, .30,
                         .35, .40, .45, .50, .55, .60, .65, .70, .75,
                         .80, .85, .90, .95, .975, .99, .995))
_DIRECTIONS = np.asarray((
    (1, 0, 0), (0, 1, 0), (0, 0, 1), (0, 1, 1), (0, 1, -1),
    (1, 1, 0), (1, -1, 0), (1, 0, 1), (1, 0, -1),
    (1, 1, 1), (1, 1, -1), (1, -1, 1), (1, -1, -1),
), dtype=np.float32)
_DIRECTIONS /= np.linalg.norm(_DIRECTIONS, axis=1, keepdims=True)


def _oklab(rgb: np.ndarray) -> np.ndarray:
    # Bjorn Ottosson's public-domain linear-sRGB -> Oklab conversion:
    # https://bottosson.github.io/posts/oklab/#converting-from-linear-srgb-to-oklab
    linear = np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)
    r, g, b = linear.T
    l = np.cbrt(.4122214708 * r + .5363325363 * g + .0514459929 * b)
    m = np.cbrt(.2119034982 * r + .6806995451 * g + .1073969566 * b)
    s = np.cbrt(.0883024619 * r + .2817188376 * g + .6299787005 * b)
    return np.column_stack((.2104542553 * l + .7936177850 * m - .0040720468 * s,
                            1.9779984951 * l - 2.4285922050 * m + .4505937099 * s,
                            .0259040371 * l + .7827717662 * m - .8086757660 * s))


def _features(rgb: np.ndarray) -> np.ndarray:
    # Joint projections distinguish warm/cool shadows and highlight casts;
    # matching each RGB histogram independently would lose that relationship.
    lab = _oklab(rgb)
    lab[:, 1:] *= 1.6
    projections = lab @ _DIRECTIONS.T
    distributions = np.quantile(projections, _QUANTILES, axis=0).ravel()
    chroma = np.quantile(np.linalg.norm(lab[:, 1:], axis=1), _QUANTILES)
    return np.concatenate((distributions, chroma))


def _sample(pixels: np.ndarray, size: int) -> np.ndarray:
    if len(pixels) > size:
        pixels = pixels[np.linspace(0, len(pixels) - 1, size, dtype=np.intp)]
    return np.ascontiguousarray(pixels, dtype=np.float32)


def _valid_pixels(pixels: np.ndarray) -> np.ndarray:
    pixels = np.asarray(pixels, dtype=np.float32)
    if pixels.ndim != 2 or pixels.shape[1] != 3:
        raise ValueError("Reference matching requires RGB color samples.")
    pixels = pixels[np.all(np.isfinite(pixels), axis=1)]
    if len(pixels) < 32:
        raise ValueError("The input or reference has too few visible colors to analyze.")
    return np.clip(pixels, 0.0, 1.0)


def _controls(vector: np.ndarray, *, quantize: bool = False) -> dict[str, int | float]:
    values = np.clip(vector, _LOW, _HIGH) * _SCALES
    return {key: (round(float(value), 2) if key == "lut_exposure" else int(round(value)))
            if quantize else float(value) for key, value in zip(_KEYS, values)}


def _fit(identity, sampled, reference, settings, initial, iterations, controller, report):
    target = _features(reference)
    weight = 1.0 / math.sqrt(len(target))
    low, high = _LOW.copy(), _HIGH.copy()

    def residual(vector):
        _check_cancel(controller)
        graded = grade_lut_rgb(identity, sampled, replace(settings, **_controls(vector)))
        # A small preference for gentle controls resolves exposure/midtones
        # and saturation/vibrance ambiguities without overriding the target.
        return np.concatenate(((_features(graded) - target) * weight,
                               vector * .00035))

    vector = np.clip(initial, low, high)
    error = residual(vector)
    score = float(error @ error)
    damping = .0001
    for iteration in range(iterations):
        _check_cancel(controller)
        jacobian = np.empty((len(error), len(vector)))
        for column in range(len(vector)):
            step = .002 if vector[column] + .002 <= high[column] else -.002
            trial = vector.copy()
            trial[column] += step
            jacobian[:, column] = (residual(trial) - error) / step
        normal = jacobian.T @ jacobian
        gradient = jacobian.T @ error
        accepted = False
        improvement = 0.0
        for _ in range(7):
            delta = np.linalg.solve(normal + np.eye(len(vector)) * damping, -gradient)
            delta = np.clip(delta, -.35, .35)
            candidate = np.clip(vector + delta, low, high)
            candidate_error = residual(candidate)
            candidate_score = float(candidate_error @ candidate_error)
            if candidate_score < score:
                improvement = score - candidate_score
                vector, error, score = candidate, candidate_error, candidate_score
                damping = max(.00000001, damping * .4)
                accepted = True
                break
            damping *= 4.0
        if report is not None:
            report((iteration + 1) / iterations)
        if not accepted or (iteration > 4 and improvement < 1e-10):
            break
    return vector, score


def match_reference_adjustments(identity: np.ndarray, sampled: np.ndarray,
                                reference: np.ndarray, settings: UISettings, *,
                                controller=None, progress=None) -> dict[str, int | float]:
    """Fit tone, white balance, hue, and saturation; preserve mix/resolution."""
    if settings.lut_mix <= 0:
        raise ValueError("Increase LUT Strength above 0 before matching a reference image.")
    identity, sampled = (np.asarray(pixels, dtype=np.float32) for pixels in (identity, sampled))
    if identity.shape != sampled.shape or identity.ndim != 2 or identity.shape[1] != 3:
        raise ValueError("Source and LUT samples must describe the same colors.")
    valid = np.all(np.isfinite(identity) & np.isfinite(sampled), axis=1)
    identity, sampled, reference = (_valid_pixels(identity[valid]), _valid_pixels(sampled[valid]),
                                    _valid_pixels(reference))
    _check_cancel(controller)
    coarse_identity, coarse_sampled, coarse_reference = (
        _sample(pixels, 2048) for pixels in (identity, sampled, reference))
    current = np.asarray([getattr(settings, key) for key in _KEYS]) / _SCALES
    neutral = np.zeros(len(_KEYS))
    seeds = [neutral, current]
    # Exposure initialization helps very dark/bright sources reach the target
    # before the finer contrast, tone zones, and color controls are fitted.
    guessed = neutral.copy()
    source_level = max(float(np.median(_oklab(coarse_sampled)[:, 0])), .03)
    target_level = max(float(np.median(_oklab(coarse_reference)[:, 0])), .03)
    guessed[0] = np.clip(3 * math.log2(target_level / source_level), -3, 3)
    seeds.append(guessed)
    best, best_score = neutral, float("inf")
    unique = []
    for seed in seeds:
        if not any(np.allclose(seed, previous) for previous in unique):
            unique.append(seed)
    for index, seed in enumerate(unique):
        def report(value, i=index):
            if progress is not None:
                progress(.20 + .40 * (i + value) / len(unique), "Matching reference tone and color")
        fitted, score = _fit(coarse_identity, coarse_sampled, coarse_reference,
                             settings, seed, 35, controller, report)
        if score < best_score:
            best, best_score = fitted, score

    fine_identity, fine_sampled, fine_reference = (
        _sample(pixels, 12_288) for pixels in (identity, sampled, reference))
    best, _ = _fit(fine_identity, fine_sampled, fine_reference, settings, best, 45,
                   controller, lambda value: progress(.60 + .35 * value, "Refining reference match")
                   if progress is not None else None)
    # Refine at the actual integer slider precision, rather than returning a
    # continuous optimum that can lose its accuracy when displayed/applied.
    values = _controls(best, quantize=True)
    target = _features(fine_reference)
    def score(values):
        _check_cancel(controller)
        difference = _features(grade_lut_rgb(fine_identity, fine_sampled,
                                             replace(settings, **values))) - target
        return float(difference @ difference)
    best_score = score(values)
    for _ in range(2):
        for key in _KEYS:
            step = .01 if key == "lut_exposure" else 1
            low, high = LUT_ADJUSTMENT_RANGES[key]
            for direction in (-1, 1):
                candidate = {**values, key: round(float(values[key]) + direction * step, 2)
                             if key == "lut_exposure" else int(values[key]) + direction}
                if low <= candidate[key] <= high:
                    candidate_score = score(candidate)
                    if candidate_score < best_score:
                        values, best_score = candidate, candidate_score
    _check_cancel(controller)
    if progress is not None:
        progress(1.0, "Reference color adjustments ready")
    return values


def analyze_color_reference(source: str | Path, reference: str | Path,
                            settings: UISettings, mode: str, *,
                            duration_seconds: float = 0.0,
                            video_size: tuple[int, int] | None = None,
                            hdr: bool = False, controller=None, progress=None) -> dict[str, int | float]:
    """Analyze the source and a decoded reference, then fit the editable grade."""
    del hdr  # Match the signal consumed by the existing grading controls.
    pixels = _source_pixels(Path(source).resolve(), mode, duration_seconds, video_size,
                            controller, lambda value, message: progress(.12 * value, message)
                            if progress is not None else None)
    _check_cancel(controller)
    if progress is not None:
        progress(.14, "Analyzing reference image")
    reference_pixels = _analysis_pixels(decode_image(Path(reference).resolve()).rgba, 220_000)
    _check_cancel(controller)
    sampled = pixels
    if settings.lut_path:
        lut = load_cube_lut(settings.lut_path)
        sampled = np.empty_like(pixels)
        for offset in range(0, len(pixels), 65_536):
            _check_cancel(controller)
            end = min(len(pixels), offset + 65_536)
            sampled[offset:end] = sample_cube_lut(pixels[offset:end], lut)
    return match_reference_adjustments(pixels, sampled, reference_pixels, settings,
                                        controller=controller, progress=progress)
