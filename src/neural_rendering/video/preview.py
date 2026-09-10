from __future__ import annotations

import traceback
from pathlib import Path

import gradio as gr

from ...core.ffmpeg.preview import (
    is_browser_playable, make_browser_preview, normalize_preview_encoding,
    resolve_final_preview, resolve_preview_codec, wants_compat_preview,
)
from ...core.preview_duration import resolve_duration
from ...settings.models import coerce_hdr_mode, parse_automatic_mask
from ...settings.storage import current_preview_encoding, processing_gpu_settings
from .models import ConversionOptions
from .processor import convert_video

def _process_video(
    input_path: str | None,
    nr_preset: str,
    nr_style: str,
    nr_intensity: float,
    local_tone_strength: float,
    local_structure_strength: float,
    skin_structure_strength: float,
    upscaling_factor: float,
    automatic_mask: str,
    dlss_model_preset: str,
    codec: str,
    container: str,
    quality: str,
    hdr_mode: bool,
    progress,
    preview_seconds: float | None,
    preview_frames: int | None,
) -> tuple[str | None, str]:
    if not input_path:
        raise gr.Error("Choose a video first.")
    is_preview = preview_seconds is not None or preview_frames is not None
    ai_gpu_uuid, video_gpu_uuid = processing_gpu_settings()
    try:
        preview_mode = current_preview_encoding()
    except Exception:
        preview_mode = "Auto"
    preview_mode = normalize_preview_encoding(preview_mode)
    if is_preview:
        effective_codec, effective_container = resolve_preview_codec(
            codec, container, preview_mode
        )
        compat_preview = wants_compat_preview(codec, container, preview_mode)
    else:
        effective_codec, effective_container = codec, container
        compat_preview = False
    # HDR Mode only for allowed codecs. Compat (forced H.264) previews stay SDR
    # 8-bit; user-encoded previews preserve the HDR choice.
    effective_hdr = coerce_hdr_mode(effective_codec, hdr_mode) and (
        not is_preview or not compat_preview
    )
    options = ConversionOptions(
        ai_gpu_uuid=ai_gpu_uuid,
        video_gpu_uuid=video_gpu_uuid,
        nr_preset=nr_preset,
        nr_style=nr_style,
        nr_intensity=nr_intensity,
        local_tone_strength=local_tone_strength,
        local_structure_strength=local_structure_strength,
        skin_structure_strength=skin_structure_strength,
        automatic_mask=parse_automatic_mask(automatic_mask),
        dlss_model_preset=dlss_model_preset,
        upscaling_factor=upscaling_factor,
        codec=effective_codec,
        container=effective_container,
        quality=quality,
        preserve_hdr=effective_hdr,
        preview_seconds=preview_seconds,
        preview_frames=preview_frames,
        preview_compat=compat_preview,
    )

    def report(value: float, message: str) -> None:
        progress(value, desc=message)

    try:
        result = convert_video(input_path, options, progress=report)
    except Exception as exc:
        traceback.print_exc()
        return None, f"Failed: {exc}"
    source_name = Path(input_path).name
    if is_preview:
        # Truncated previews normally show the encoded file directly. In Auto
        # mode the pre-check may have selected the user encoding but the actual
        # file can still probe as unplayable (rare); then derive one H.264 file.
        output_preview = result.output_path
        derived_note = ""
        if preview_mode == "Auto" and not compat_preview:
            try:
                playable = is_browser_playable(result.output_path)
            except Exception:
                playable = False
            if not playable:
                try:
                    output_preview = make_browser_preview(result.output_path)
                    derived_note = " (browser preview transcoded to H.264)"
                except Exception:
                    output_preview = result.output_path
        if preview_frames is not None:
            return output_preview, (
                f"One-frame preview complete for {source_name} on {result.gpu} "
                f"in {result.elapsed_seconds:.1f}s. "
                f"DLSS {result.dlss_mode}: {result.render_width}×{result.render_height} → "
                f"{result.output_width}×{result.output_height}. Signed feature 18 confirmed."
                f"{derived_note}"
            )
        return output_preview, (
            f"Preview complete for {source_name}: {result.frames} frames from the first "
            f"{preview_seconds:g} seconds processed "
            f"on {result.gpu} in {result.elapsed_seconds:.1f}s. DLSS {result.dlss_mode}: "
            f"{result.render_width}×{result.render_height} → {result.output_width}×{result.output_height}. "
            "All frames returned success with signed feature 18 confirmed."
            f"{derived_note}"
        )
    output_preview, used_derivative = resolve_final_preview(
        result.output_path, preview_mode
    )
    status = (
        f"Complete: {result.frames} frames processed on {result.gpu} in {result.elapsed_seconds:.1f}s. "
        f"All {result.nr_count_evidence} frames returned success with signed feature 18 confirmed. "
        f"DLSS {result.dlss_mode}: {result.render_width}×{result.render_height} → "
        f"{result.output_width}×{result.output_height}."
    )
    if used_derivative:
        status += " Browser preview transcoded to H.264; the original file is unchanged."
    elif output_preview is None:
        status += f" {effective_container} output was created successfully, but browser preview is unavailable."
    return output_preview, status

def normalize_video_paths(paths: list[str] | str | None) -> list[str]:
    if not paths:
        return []
    return [paths] if isinstance(paths, str) else list(paths)


def first_video_path(paths: list[str] | str | None) -> str | None:
    normalized = normalize_video_paths(paths)
    return normalized[0] if normalized else None


def update_video_preview_mode(paths: list[str] | str | None):
    normalized = normalize_video_paths(paths)
    available = bool(normalized)
    single = len(normalized) == 1
    input_value = normalized[0] if available else None
    input_label = "Input video preview" if len(normalized) <= 1 else f"Input video preview (first of {len(normalized)})"
    return (
        gr.update(value=input_value, visible=available, label=input_label),
        gr.update(value=None, visible=True),
        gr.update(visible=single),
        gr.update(visible=single),
    )

def preview_with_duration(
    input_path: list[str] | str | None,
    nr_preset: str,
    nr_style: str,
    nr_intensity: float,
    local_tone_strength: float,
    local_structure_strength: float,
    skin_structure_strength: float,
    upscaling_factor: float,
    automatic_mask: str,
    dlss_model_preset: str,
    codec: str,
    container: str,
    quality: str,
    hdr_mode: bool,
    duration: str,
    progress=gr.Progress(track_tqdm=False),
):
    selected = first_video_path(input_path)
    preview_seconds, preview_frames = resolve_duration(duration)
    return _process_video(
        selected, nr_preset, nr_style, nr_intensity, local_tone_strength, local_structure_strength,
        skin_structure_strength, upscaling_factor, automatic_mask, dlss_model_preset,
        codec, container, quality, hdr_mode,
        progress, preview_seconds, preview_frames
    )
