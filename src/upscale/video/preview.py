from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import gradio as gr

from ...core.ffmpeg.preview import is_browser_playable, make_browser_preview
from ...core.preview_duration import DEFAULT_DURATION, resolve_duration
from ...settings.storage import current_preview_encoding
from .media import inspect_video
from .processor import upscale_video


def display_result(result, options, controller=None):
    mode = current_preview_encoding()
    if mode == "Disabled":
        return result.output_path, "Actual output; browser HDR/codec support depends on your display and browser."
    if not options.hdr_enabled and mode == "Auto" and is_browser_playable(result.output_path):
        return result.output_path, ""
    vf = None
    if options.hdr_enabled:
        peak = options.hdr_peak_luminance / 100
        vf = ("zscale=matrixin=bt2020nc:primariesin=bt2020:transferin=smpte2084:rangein=limited:"
              "transfer=linear:npl=100,format=gbrpf32le,zscale=primaries=bt709,"
              f"tonemap=mobius:desat=2:peak={peak:g},zscale=transfer=bt709:matrix=bt709:range=limited,format=yuv420p")
    try:
        path = make_browser_preview(result.output_path, controller=controller, sdr_filter=vf)
    except Exception as exc:
        return None, f"Output saved; browser preview unavailable: {exc}"
    return path, "SDR tone-mapped browser preview. Download the original for HDR playback." if options.hdr_enabled else "H.264 browser preview."


def preview_upscale(paths, options, *, duration=DEFAULT_DURATION, progress=None):
    paths = [paths] if isinstance(paths, str) else list(paths or [])
    if len(paths) != 1:
        raise gr.Error("Select one video to preview.")
    from ...core.jobs import current_job_controller
    controller = current_job_controller()
    preview_seconds, preview_frames = resolve_duration(duration)
    opts = replace(options, preview_frames=preview_frames, preview_seconds=preview_seconds)
    result = upscale_video(paths[0], opts, progress=progress, controller=controller)
    display, detail = display_result(result, opts, controller)
    return gr.update(value=display, visible=True, label="SDR tone-mapped preview (download original for HDR)" if opts.hdr_enabled and current_preview_encoding() != "Disabled" else "Output preview"), (
        f"Preview complete: {result.output_width}×{result.output_height}, {result.frames} frames.\n{detail}\n"
        f"Original preview file: {result.output_path}\nReport: {result.report_path}")


def preview_mode(paths):
    paths = [paths] if isinstance(paths, str) else list(paths or [])
    available = bool(paths)
    single = len(paths) == 1
    label = "Input video preview" if len(paths) <= 1 else f"Input video preview (first of {len(paths)})"
    return (gr.update(value=paths[0] if available else None, visible=available, label=label),
            gr.update(value=None, visible=True), gr.update(visible=single), gr.update(visible=single))
