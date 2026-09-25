"""User-facing DLSS Super Resolution modes and output sizing."""

from __future__ import annotations

import math

# Each media frame is evaluated once. Repeating a resolved still cannot
# reproduce the new geometry samples supplied by a jittered game renderer.
DLSS_MODES = {
    "DLAA": (1.0, 1),
    "Quality": (1.5, 1),
    "Balanced": (1.724, 1),
    "Performance": (2.0, 1),
    "Ultra Performance": (3.0, 1),
}
DLSS_PRESETS = ("Default", "J", "K", "L", "M")
DLSS_METHODS = ("Standard", "DLSS")
UPSCALE_ENGINES = ("RTX Video Super Resolution", "DLSS")


def validate_dlss(mode: str, preset: str) -> None:
    if mode not in DLSS_MODES:
        raise ValueError(f"Unknown DLSS mode: {mode!r}.")
    if preset not in DLSS_PRESETS:
        raise ValueError(f"Unknown DLSS preset: {preset!r}.")


def dlss_output_size(width: int, height: int, mode: str, *, even: bool = False) -> tuple[int, int]:
    if mode not in DLSS_MODES:
        raise ValueError(f"Unknown DLSS mode: {mode!r}.")
    if width < 32 or height < 32:
        raise ValueError("DLSS input must be at least 32×32 pixels.")
    factor = DLSS_MODES[mode][0]
    rounder = (lambda value: max(2, math.ceil(value / 2) * 2)) if even else math.ceil
    output = rounder(width * factor), rounder(height * factor)
    if max(output) > 16384:
        raise ValueError("DLSS output exceeds the 16384-pixel texture limit.")
    return output
