from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import gradio as gr

from ...core.ffmpeg.preview import is_browser_playable, make_browser_preview
from ...core.i18n import t
from ...settings.storage import current_preview_encoding
from .media import inspect_video
from .processor import upscale_video


def display_result(result, options, controller=None):
    mode = current_preview_encoding()
    if mode == "Disabled":
        return result.output_path, t("upscale.video.status.actual_output")
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
        return None, t("upscale.video.status.preview_unavailable", error=exc)
    return path, t("upscale.video.status.preview_sdr") if options.hdr_enabled else t("upscale.video.status.preview_h264")


def preview_upscale(paths, options, *, one_frame=False, progress=None):
    paths = [paths] if isinstance(paths, str) else list(paths or [])
    if len(paths) != 1:
        raise gr.Error(t("upscale.video.error.select_one_video"))
    from ...core.jobs import current_job_controller
    controller = current_job_controller()
    opts = replace(options, preview_frames=1 if one_frame else None, preview_seconds=None if one_frame else 3.0)
    result = upscale_video(paths[0], opts, progress=progress, controller=controller)
    display, detail = display_result(result, opts, controller)
    return gr.update(value=display, visible=True, label=t("upscale.video.label.output_sdr_preview") if opts.hdr_enabled and current_preview_encoding() != "Disabled" else t("upscale.video.label.output_preview")), (
        t(
            "upscale.video.status.preview_complete",
            width=result.output_width,
            height=result.output_height,
            frames=result.frames,
            detail=detail,
            path=result.output_path,
            report=result.report_path,
        )
    )


def preview_mode(paths):
    paths = [paths] if isinstance(paths, str) else list(paths or [])
    available = bool(paths)
    single = len(paths) == 1
    label = t("common.label.input_video_preview") if len(paths) <= 1 else t("neural.video.label.input_preview_first", count=len(paths))
    return (gr.update(value=paths[0] if available else None, visible=True if available else "hidden", label=label),
            gr.update(value=None, visible=True), gr.update(visible=single), gr.update(visible=single))
