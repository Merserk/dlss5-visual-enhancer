"""Shared preview-duration control for the render-capable video tabs.

Neural Rendering Video, Upscale Video, and Frame Interpolation each let you
render a short preview instead of the full video before committing to a real
render. This used to be two separate hardcoded buttons per tab ("Preview 1
frame" / "Preview 3 sec"); this module gives them one consistent duration
dropdown + a single Preview button instead.
"""
from __future__ import annotations

import gradio as gr

# Neural Rendering Video and Upscale Video can preview a single still frame
# (useful for judging static per-pixel detail) in addition to a timed clip.
DURATION_CHOICES = ("1 frame", "3 seconds", "5 seconds", "10 seconds")

# Frame Interpolation has nothing to show from a single frame -- there is no
# interpolation to see without at least two source frames' worth of playback
# -- so it only ever offers timed clips.
DURATION_CHOICES_TIMED_ONLY = ("3 seconds", "5 seconds", "10 seconds")

DEFAULT_DURATION = "3 seconds"

_SECONDS_BY_CHOICE = {"3 seconds": 3.0, "5 seconds": 5.0, "10 seconds": 10.0}


def build_duration_control(*, allow_single_frame: bool = True) -> gr.Dropdown:
    """Build the duration dropdown. Starts hidden; the tab's own preview_mode
    refresh callback shows it exactly when the existing preview button(s)
    would have shown, same as before."""
    choices = DURATION_CHOICES if allow_single_frame else DURATION_CHOICES_TIMED_ONLY
    return gr.Dropdown(
        choices=list(choices), value=DEFAULT_DURATION, label="Preview duration",
        info="How much to render for a quick look before committing to a full render.",
        visible=False,
    )


def resolve_duration(choice: str) -> tuple[float | None, int | None]:
    """Map a duration-dropdown choice to (preview_seconds, preview_frames)."""
    if choice == "1 frame":
        return None, 1
    return _SECONDS_BY_CHOICE.get(choice, _SECONDS_BY_CHOICE[DEFAULT_DURATION]), None
